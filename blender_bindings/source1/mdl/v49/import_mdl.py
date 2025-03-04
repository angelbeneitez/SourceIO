import math
import sys
import warnings
from pathlib import Path

import bpy

import numpy as np
from mathutils import Vector, Matrix, Euler, Quaternion

from .. import FileImport
from ..common import get_slice, merge_meshes
from .....library.utils.byte_io_mdl import ByteIO
from .....logger import SLoggingManager
from .....library.shared.content_providers.content_manager import ContentManager

from .....library.source1.vvd import Vvd

from .....library.source1.vtx.v7.vtx import Vtx

from .....library.source1.mdl.v49.mdl_file import MdlV49
from .....library.source1.mdl.structs.header import StudioHDRFlags
from .....library.source1.mdl.v44.vertex_animation_cache import VertexAnimationCache
from .....library.source1.mdl.v49.flex_expressions import *

from ....shared.model_container import Source1ModelContainer
from ....material_loader.material_loader import Source1MaterialLoader
from ....material_loader.shaders.source1_shader_base import Source1ShaderBase
from ....utils.utils import get_material
from .....library.utils.math_utilities import euler_to_quat
# from .....library.utils.pylib_loader import source1

log_manager = SLoggingManager()
logger = log_manager.get_logger('Source1::ModelLoader')


def collect_full_material_names(mdl: MdlV49):
    content_manager = ContentManager()
    full_mat_names = {}
    for material_path in mdl.materials_paths:
        for material in mdl.materials:
            real_material_path = content_manager.find_material(Path(material_path) / material.name)
            if real_material_path is not None:
                full_mat_names[material] = str(Path(material_path) / material.name)
    return full_mat_names


def create_armature(mdl: MdlV49, scale=1.0):
    model_name = Path(mdl.header.name).stem
    armature = bpy.data.armatures.new(f"{model_name}_ARM_DATA")
    armature_obj = bpy.data.objects.new(f"{model_name}_ARM", armature)
    armature_obj['MODE'] = 'SourceIO'
    armature_obj.show_in_front = True
    bpy.context.scene.collection.objects.link(armature_obj)

    armature_obj.select_set(True)
    bpy.context.view_layer.objects.active = armature_obj

    bpy.ops.object.mode_set(mode='EDIT')
    bl_bones = []
    for bone in mdl.bones:
        bl_bone = armature.edit_bones.new(bone.name[-63:])
        bl_bones.append(bl_bone)

    for bl_bone, s_bone in zip(bl_bones, mdl.bones):
        if s_bone.parent_bone_index != -1:
            bl_parent = bl_bones[s_bone.parent_bone_index]
            bl_bone.parent = bl_parent
        bl_bone.tail = (Vector([0, 0, 1]) * scale) + bl_bone.head

    bpy.ops.object.mode_set(mode='POSE')
    for n, se_bone in enumerate(mdl.bones):
        bl_bone = armature_obj.pose.bones.get(se_bone.name[-63:])
        pos = Vector(se_bone.position) * scale
        rot = Euler(se_bone.rotation)
        mat = Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
        bl_bone.matrix_basis.identity()

        bl_bone.matrix = bl_bone.parent.matrix @ mat if bl_bone.parent else mat
    bpy.ops.pose.armature_apply()
    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.context.scene.collection.objects.unlink(armature_obj)
    return armature_obj


def import_model(file_list: FileImport,
                 scale=1.0, create_drivers=False, re_use_meshes=False, unique_material_names=False):
    mdl = MdlV49(file_list.mdl_file)
    mdl.read()

    full_material_names = collect_full_material_names(mdl)

    vvd = Vvd(file_list.vvd_file)
    vvd.read()
    vtx = Vtx(file_list.vtx_file)
    vtx.read()

    container = Source1ModelContainer(mdl, vvd, vtx, file_list)

    desired_lod = 0
    all_vertices = vvd.lod_data[desired_lod]

    static_prop = mdl.header.flags & StudioHDRFlags.STATIC_PROP != 0
    armature = None
    if mdl.flex_names:
        vac = VertexAnimationCache(mdl, vvd)
        vac.process_data()

    if not static_prop:
        armature = create_armature(mdl, scale)
        container.armature = armature

    for vtx_body_part, body_part in zip(vtx.body_parts, mdl.body_parts):
        for vtx_model, model in zip(vtx_body_part.models, body_part.models):

            if model.vertex_count == 0:
                continue
            mesh_name = f'{body_part.name}_{model.name}'
            used_copy = False
            if re_use_meshes:  # and static_prop:
                mesh_obj_original = bpy.data.objects.get(mesh_name, None)
                mesh_data_original = bpy.data.meshes.get(f'{mdl.header.name}_{mesh_name}_MESH', False)
                if mesh_obj_original and mesh_data_original:
                    mesh_data = mesh_data_original  # .copy()
                    mesh_obj = mesh_obj_original.copy()
                    mesh_obj['skin_groups'] = mesh_obj_original['skin_groups']
                    mesh_obj['active_skin'] = mesh_obj_original['active_skin']
                    mesh_obj['model_type'] = 's1'
                    mesh_obj.data = mesh_data
                    used_copy = True
                else:
                    mesh_data = bpy.data.meshes.new(f'{mesh_name}_MESH')
                    mesh_obj = bpy.data.objects.new(mesh_name, mesh_data)
                    mesh_obj['skin_groups'] = {str(n): group for (n, group) in enumerate(mdl.skin_groups)}
                    mesh_obj['active_skin'] = '0'
                    mesh_obj['model_type'] = 's1'
            else:
                mesh_data = bpy.data.meshes.new(f'{mesh_name}_MESH')
                mesh_obj = bpy.data.objects.new(mesh_name, mesh_data)
                mesh_obj['skin_groups'] = {str(n): group for (n, group) in enumerate(mdl.skin_groups)}
                mesh_obj['active_skin'] = '0'
                mesh_obj['model_type'] = 's1'

            if not static_prop:
                modifier = mesh_obj.modifiers.new(
                    type="ARMATURE", name="Armature")
                modifier.object = armature
                mesh_obj.parent = armature
            container.objects.append(mesh_obj)
            container.bodygroups[body_part.name].append(mesh_obj)
            mesh_obj['unique_material_names'] = unique_material_names
            mesh_obj['prop_path'] = Path(mdl.header.name).stem

            if used_copy:
                continue

            model_vertices = get_slice(all_vertices, model.vertex_offset, model.vertex_count)
            vtx_vertices, indices_array, material_indices_array = merge_meshes(model, vtx_model.model_lods[desired_lod])

            indices_array = np.array(indices_array, dtype=np.uint32)
            vertices = model_vertices[vtx_vertices]
            vertices_vertex = vertices['vertex']

            mesh_data.from_pydata(vertices_vertex * scale, [], np.flip(indices_array).reshape((-1, 3)).tolist())
            mesh_data.update()

            mesh_data.polygons.foreach_set("use_smooth", np.ones(len(mesh_data.polygons), np.uint32))
            mesh_data.normals_split_custom_set_from_vertices(vertices['normal'])
            mesh_data.use_auto_smooth = True

            material_remapper = np.zeros((material_indices_array.max() + 1,), dtype=np.uint32)
            for mat_id in np.unique(material_indices_array):
                mat_name = mdl.materials[mat_id].name
                if unique_material_names:
                    mat_name = f"{Path(mdl.header.name).stem}_{mat_name[-63:]}"[-63:]
                else:
                    mat_name = mat_name[-63:]
                material_remapper[mat_id] = get_material(mat_name, mesh_obj)

            mesh_data.polygons.foreach_set('material_index', material_remapper[material_indices_array[::-1]].tolist())

            vertex_indices = np.zeros((len(mesh_data.loops, )), dtype=np.uint32)
            mesh_data.loops.foreach_get('vertex_index', vertex_indices)

            uv_data = mesh_data.uv_layers.new()
            uvs = vertices['uv']
            uvs[:, 1] = 1 - uvs[:, 1]
            uv_data.data.foreach_set('uv', uvs[vertex_indices].flatten())

            if vvd.extra_data:
                for extra_type, extra_data in vvd.extra_data.items():
                    extra_data = extra_data.reshape((-1, 2))
                    extra_uv = get_slice(extra_data, model.vertex_offset, model.vertex_count)
                    extra_uv = extra_uv[vtx_vertices]
                    uv_data = mesh_data.uv_layers.new(name=extra_type.name)
                    extra_uv[:, 1] = 1 - extra_uv[:, 1]
                    uv_data.data.foreach_set('uv', extra_uv[vertex_indices].flatten())

            if not static_prop:
                weight_groups = {bone.name: mesh_obj.vertex_groups.new(name=bone.name) for bone in mdl.bones}

                for n, (bone_indices, bone_weights) in enumerate(zip(vertices['bone_id'], vertices['weight'])):
                    for bone_index, weight in zip(bone_indices, bone_weights):
                        if weight > 0:
                            bone_name = mdl.bones[bone_index].name
                            weight_groups[bone_name].add([n], weight, 'REPLACE')

            if not static_prop:
                flexes = []
                for mesh in model.meshes:
                    if mesh.flexes:
                        flexes.extend([(mdl.flex_names[flex.flex_desc_index], flex) for flex in mesh.flexes])

                if flexes:
                    mesh_obj.shape_key_add(name='base')
                    for flex_name, flex_desc in flexes:
                        vertex_animation = vac.vertex_cache[flex_name]
                        flex_delta = get_slice(vertex_animation, model.vertex_offset, model.vertex_count)
                        flex_delta = flex_delta[vtx_vertices] * scale
                        model_vertices = get_slice(all_vertices['vertex'], model.vertex_offset, model.vertex_count)
                        model_vertices = model_vertices[vtx_vertices] * scale

                        if create_drivers and flex_desc.partner_index:
                            partner_name = mdl.flex_names[flex_desc.partner_index]
                            partner_shape_key = (mesh_data.shape_keys.key_blocks.get(partner_name, None) or
                                                 mesh_obj.shape_key_add(name=partner_name))
                            shape_key = (mesh_data.shape_keys.key_blocks.get(flex_name, None) or
                                         mesh_obj.shape_key_add(name=flex_name))

                            balance = model_vertices[:, 0]
                            balance_width = (model_vertices.max() - model_vertices.min()) * (1 - (99.3 / 100))
                            balance = np.clip((-balance / balance_width / 2) + 0.5, 0, 1)

                            flex_vertices = (flex_delta * balance[:, None]) + model_vertices
                            shape_key.data.foreach_set("co", flex_vertices.reshape(-1))

                            p_balance = 1 - balance
                            p_flex_vertices = (flex_delta * p_balance[:, None]) + model_vertices
                            partner_shape_key.data.foreach_set("co", p_flex_vertices.reshape(-1))
                        else:
                            shape_key = mesh_data.shape_keys.key_blocks.get(flex_name, None) or mesh_obj.shape_key_add(
                                name=flex_name)

                            shape_key.data.foreach_set("co", (flex_delta + model_vertices).reshape(-1))
                    if create_drivers:
                        create_flex_drivers(mesh_obj, mdl)
    if mdl.attachments:
        attachments = create_attachments(mdl, armature if not static_prop else container.objects[0], scale)
        container.attachments.extend(attachments)

    return container


def create_flex_drivers(obj, mdl: MdlV49):
    from ....operators.flex_operators import SourceIO_PG_FlexController
    if not obj.data.shape_keys:
        return
    all_exprs = mdl.rebuild_flex_rules()
    data = obj.data
    shape_key_block = data.shape_keys

    def _parse_simple_flex(missing_flex_name: str):
        flexes = missing_flex_name.split('_')
        if not all(flex in data.flex_controllers for flex in flexes):
            return None
        return Combo([FetchController(flex) for flex in flexes]), [(flex, 'fetch2') for flex in flexes]

    st = '\n    '

    for flex_controller_ui in mdl.flex_ui_controllers:
        cont: SourceIO_PG_FlexController = data.flex_controllers.add()

        if flex_controller_ui.nway_controller:
            nway_cont: SourceIO_PG_FlexController = data.flex_controllers.add()
            nway_cont.stereo = False
            multi_controller = next(filter(lambda a: a.name == flex_controller_ui.nway_controller, mdl.flex_controllers)
                                    )
            nway_cont.name = flex_controller_ui.nway_controller
            nway_cont.set_from_controller(multi_controller)

        if flex_controller_ui.stereo:
            left_controller = next(
                filter(lambda a: a.name == flex_controller_ui.left_controller, mdl.flex_controllers)
            )
            right_controller = next(
                filter(lambda a: a.name == flex_controller_ui.right_controller, mdl.flex_controllers)
            )
            cont.stereo = True
            cont.name = flex_controller_ui.name
            assert left_controller.max == right_controller.max
            assert left_controller.min == right_controller.min
            cont.set_from_controller(left_controller)
        else:
            controller = next(filter(lambda a: a.name == flex_controller_ui.controller, mdl.flex_controllers))
            cont.stereo = False
            cont.name = flex_controller_ui.name
            cont.set_from_controller(controller)
    blender_py_file = """
import bpy

def rclamped(val, a, b, c, d):
    if ( a == b ):
        return d if val >= b else c;
    return c + (d - c) * min(max((val - a) / (b - a), 0.0), 1.0)
    
def clamp(val, a, b):
    return min(max(val, a), b)

def nway(multi_value, flex_value, x, y, z, w):
    if multi_value <= x or multi_value >= w:  # outside of boundaries
        multi_value = 0.0
    elif multi_value <= y:
        multi_value = rclamped(multi_value, x, y, 0.0, 1.0)
    elif multi_value >= z:
        multi_value = rclamped(multi_value, z, w, 1.0, 0.0)
    else:
        multi_value = 1.0
    return multi_value * flex_value


def combo(*values):
    val = values[0]
    for v in values[1:]:
        val*=v
    return val
    
def dom(dm, *values):
    val = 1
    for v in values:
        val *= v
    return val * (1 - dm)

def lower_eyelid_case(eyes_up_down,close_lid_v,close_lid):
    if eyes_up_down > 0.0:
        return (1.0 - eyes_up_down) * (1.0 - close_lid_v) * close_lid
    else:
        return  (1.0 - close_lid_v) * close_lid

def upper_eyelid_case(eyes_up_down,close_lid_v,close_lid):
    if eyes_up_down > 0.0:
        return (1.0 + eyes_up_down) * close_lid_v * close_lid
    else:
        return  close_lid_v * close_lid


bpy.app.driver_namespace["combo"] = combo
bpy.app.driver_namespace["dom"] = dom
bpy.app.driver_namespace["nway"] = nway
bpy.app.driver_namespace["rclamped"] = rclamped

    """
    for flex_name, (expr, inputs) in all_exprs.items():
        driver_name = f'{flex_name}_driver'.replace(' ', '_')
        if driver_name in globals():
            continue

        input_definitions = []
        for inp in inputs:
            input_name = inp[0]
            if inp[1] in ('fetch1', '2WAY1', '2WAY0', 'NWAY', 'DUE'):
                if 'left_' in input_name:
                    input_definitions.append(
                        f'{inp[0].replace(" ", "_")} = obj_data.flex_controllers["{input_name.replace("left_", "")}"].value_left')
                elif 'right_' in input_name:
                    input_definitions.append(
                        f'{inp[0].replace(" ", "_")} = obj_data.flex_controllers["{input_name.replace("right_", "")}"].value_right')
                else:
                    input_definitions.append(
                        f'{inp[0].replace(" ", "_")} = obj_data.flex_controllers["{inp[0]}"].value')
            elif inp[1] == 'fetch2':
                input_definitions.append(
                    f'{inp[0].replace(" ", "_")} = obj_data.shape_keys.key_blocks["{input_name}"].value')
            else:
                raise NotImplementedError(f'"{inp[1]}" is not supported')
        print(f"{flex_name} = {expr}")
        template_function = f"""
def {driver_name}(obj_data):
    {st.join(input_definitions)}
    return {expr}
bpy.app.driver_namespace["{driver_name}"] = {driver_name}

"""
        blender_py_file += template_function

    for shape_key in shape_key_block.key_blocks:

        flex_name = shape_key.name

        if flex_name == 'base':
            continue
        if flex_name not in all_exprs:
            warnings.warn(f'Rule for {flex_name} not found! Generating basic rule.')
            expr, inputs = _parse_simple_flex(flex_name) or (None, None)
            if not expr or not inputs:
                warnings.warn(f'Failed to generate basic rule for {flex_name}!')
                cont: SourceIO_PG_FlexController = data.flex_controllers.add()
                cont.name = flex_name
                cont.mode = 1
                cont.value_min = 0
                cont.value_max = 1
                template_function = f"""
def {flex_name.replace(' ', '_')}_driver(obj_data):
    return obj_data.flex_controllers["{flex_name}"].value
bpy.app.driver_namespace["{flex_name.replace(' ', '_')}_driver"] = {flex_name.replace(' ', '_')}_driver

                                """
                blender_py_file += template_function
            else:
                template_function = f"""
def {flex_name.replace(' ', '_')}_driver(obj_data):
    {st.join(inputs)}
    return {expr}
bpy.app.driver_namespace["{flex_name.replace(' ', '_')}_driver"] = {flex_name.replace(' ', '_')}_driver

                """
                blender_py_file += template_function

        shape_key.driver_remove("value")
        fcurve = shape_key.driver_add("value")
        fcurve.modifiers.remove(fcurve.modifiers[0])

        driver = fcurve.driver
        driver.type = 'SCRIPTED'
        driver.expression = f"{flex_name.replace(' ', '_')}_driver(obj_data)"
        var = driver.variables.new()
        var.name = 'obj_data'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = obj
        var.targets[0].data_path = f"data"

    driver_file = bpy.data.texts.new(f'{mdl.header.name}.py')
    driver_file.write(blender_py_file)
    driver_file.use_module = True


def create_attachments(mdl: MdlV49, armature: bpy.types.Object, scale):
    attachments = []
    for attachment in mdl.attachments:
        empty = bpy.data.objects.new(attachment.name, None)
        pos = Vector(attachment.pos) * scale
        rot = Euler(attachment.rot)

        empty.matrix_basis.identity()
        empty.scale *= scale
        empty.location = pos
        empty.rotation_euler = rot

        if armature.type == 'ARMATURE':
            modifier = empty.constraints.new(type="CHILD_OF")
            modifier.target = armature
            modifier.subtarget = mdl.bones[attachment.parent_bone].name
            modifier.inverse_matrix.identity()

        attachments.append(empty)

    return attachments


def import_materials(mdl: MdlV49, unique_material_names=False, use_bvlg=False):
    content_manager = ContentManager()
    for material in mdl.materials:

        if unique_material_names:
            mat_name = f"{Path(mdl.header.name).stem}_{material.name[-63:]}"[-63:]
        else:
            mat_name = material.name[-63:]
        material_eyeball = None
        for eyeball in mdl.eyeballs:
            if eyeball.material.name == material.name:
                material_eyeball = eyeball

        if bpy.data.materials.get(mat_name, False):
            if bpy.data.materials[mat_name].get('source1_loaded', False):
                logger.info(f'Skipping loading of {mat_name} as it already loaded')
                continue
        material_path = None
        for mat_path in mdl.materials_paths:
            material_path = content_manager.find_material(Path(mat_path) / material.name)
            if material_path:
                break
        if material_path:
            Source1ShaderBase.use_bvlg(use_bvlg)
            if material_eyeball is not None:
                pass
                # TODO: Syborg64 replace this with actual shader class
                # new_material = EyeShader(material_path, mat_name, material_eyeball)
                new_material = Source1MaterialLoader(material_path, mat_name)
            else:
                new_material = Source1MaterialLoader(material_path, mat_name)
            new_material.create_material()


def import_animations(mdl_file: ByteIO, mdl: MdlV49, armature, scale):
    return
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='POSE')
    if not armature.animation_data:
        armature.animation_data_create()
    mdl_file.seek(0)
    buffer = mdl_file.read(-1)
    mdl_resource = source1.MdlResource(data_buffer=buffer)
    if mdl_resource.animation_count == 0:
        return
    ref_animation = mdl_resource.get_animation(0, True)
    ref_matrices = []
    for bone_id, bone in enumerate(mdl.bones):
        pos, rot = ref_animation.get_frame_bone_data(0, bone_id)
        pos = Vector(pos)
        rot = Quaternion(rot)
        ref_matrix = Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
        b = armature.data.bones.get(bone.name)

        ref_matrices.append(ref_matrix)
        # ref_matrices.append(b.matrix_local.inverted() @ ref_matrix )
    for n in range(0, 1):  # mdl_resource.animation_count):
        animation = mdl_resource.get_animation(n, n == 0)
        action = bpy.data.actions.new(animation.name)
        armature.animation_data.action = action
        curve_per_bone = {}

        for bone in mdl.bones:
            bone_name = bone.name
            bl_bone = armature.pose.bones.get(bone_name)
            bl_bone.rotation_mode = 'QUATERNION'
            bone_string = f'pose.bones["{bone_name}"].'
            group = action.groups.new(name=bone_name)
            pos_curves = []
            rot_curves = []
            for i in range(3):
                pos_curve = action.fcurves.new(data_path=bone_string + "location", index=i)
                pos_curve.keyframe_points.add(animation.frame_count)
                pos_curves.append(pos_curve)
                pos_curve.group = group
            for i in range(4):
                rot_curve = action.fcurves.new(data_path=bone_string + "rotation_quaternion", index=i)
                rot_curve.keyframe_points.add(animation.frame_count)
                rot_curves.append(rot_curve)
                rot_curve.group = group
            curve_per_bone[bone_name] = pos_curves, rot_curves
        for bone_id, bone in enumerate(mdl.bones):
            for frame_id in range(animation.frame_count):
                ebone = armature.data.bones.get(bone.name)
                bl_bone = armature.pose.bones.get(bone.name)
                pos_curves, rot_curves = curve_per_bone[bone.name]
                pos, rot = animation.get_frame_bone_data(frame_id, bone_id)


                obj = bpy.data.objects.new(f'{animation.name}_{frame_id}_{bone.name}', None)
                obj.empty_display_type = 'ARROWS'
                obj.empty_display_size = 3.29

                obj.location = pos
                obj.rotation_mode = 'QUATERNION'
                obj.rotation_quaternion = rot
                bpy.context.scene.collection.objects.link(obj)
                print(f"Local space Frame: {frame_id:<5} Bone:{bone.name:<25} |  {pos}  {rot}")
                # x, y, z = pos
                # pos = -x, -y, z
                # w, x, y, z = rot
                # rot = w, -x, -z, -y
                pos = Vector(pos)
                rot = Quaternion(rot)
                mat = Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
                # mat = ebone.matrix_local.inverted() @ mat
                # mat @= Matrix.Rotation(math.radians(90), 4, 'X')
                # mat @= Matrix.Rotation(math.radians(90), 4, 'Y')

                # if not bl_bone.parent:
                # mat = ebone.matrix_local.inverted() @ mat
                # mat = mat @ Matrix.Rotation(math.radians(90), 4, 'Y')
                # pass
                # else:
                #         mat = ebone.parent.matrix_local.inverted() @ mat
                #     mat = ebone.matrix_local.inverted() @ mat
                # if not bl_bone.parent:
                #     mat @= Matrix.Rotation(math.radians(90), 4, 'X')
                #     # mat @= Matrix.Rotation(math.radians(180), 4, 'Z')
                # else:
                #     mat @= Matrix.Rotation(math.radians(180), 4, 'Z')
                # bl_bone.matrix = mat
                # mat = bl_bone.matrix_basis
                # mat = ref_matrices[bone_id].inverted() @ mat

                # if bl_bone.parent:
                #     mat = ebone.convert_local_to_pose(mat, ref_matrices[bone_id],
                #                                       # parent_matrix=bl_bone.parent.matrix,
                #                                       # parent_matrix_local=ref_matrices[bone.parent_bone_index],
                #                                       invert=False)
                # else:
                #     mat = ebone.convert_local_to_pose(mat, ref_matrices[bone_id], invert=False)

                pos, rot, scl = mat.decompose()
                print(f"Pose space  Frame: {frame_id:<5} Bone:{bone.name:<25} |  {pos}  {rot}")
                for i in range(3):
                    pos_curves[i].keyframe_points.add(1)
                    pos_curves[i].keyframe_points[-1].co = (frame_id, (pos[i]) * scale)

                for i in range(4):
                    rot_curves[i].keyframe_points.add(1)
                    rot_curves[i].keyframe_points[-1].co = (frame_id, (rot[i]))
        bpy.ops.object.mode_set(mode='OBJECT')
