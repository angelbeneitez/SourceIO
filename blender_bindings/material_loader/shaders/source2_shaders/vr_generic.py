import numpy as np

from ..source2_shader_base import Source2ShaderBase
from ...shader_base import Nodes


class VRGeneric(Source2ShaderBase):
    SHADER: str = 'vr_standard.vfx'

    @property
    def color_texture(self):
        texture_path = self.get_texture('g_tColor', None)
        if texture_path is not None:
            image = self.load_texture_or_default(texture_path, (0.3, 0.3, 0.3, 1.0))
            return image
        return None

    @property
    def ambient_occlusion(self):
        texture_path = self.get_texture('g_tAmbientOcclusion', None)
        if texture_path is not None:
            image = self.load_texture_or_default(texture_path, (1.0, 1.0, 1.0, 1.0))
            image.colorspace_settings.is_data = True
            image.colorspace_settings.name = 'Non-Color'
            return image
        return None

    @property
    def normal_texture(self):
        texture_path = self.get_texture('g_tNormal', None)
        if texture_path is not None:
            image = self.load_texture_or_default(texture_path, (0.5, 0.5, 1.0, 1.0))
            image.colorspace_settings.is_data = True
            image.colorspace_settings.name = 'Non-Color'
            image, roughness = self.split_normal(image)
            return image, roughness
        return None, None

    @property
    def color(self):
        return self.get_vector('g_vColorTint', np.ones(4, dtype=np.float32))

    @property
    def alpha_test(self):
        return self.get_int('F_ALPHA_TEST', 0)

    @property
    def metalness(self):
        return self.get_int('F_METALNESS_TEXTURE', 0)

    @property
    def translucent(self):
        return self.get_int('F_TRANSLUCENT', 0)

    @property
    def specular(self):
        return self.get_vector('g_vGlossinessRange', [0, 1, 0, 0])[0]

    @property
    def roughness(self):
        return self.get_vector('g_vGlossinessRange', [0, 1, 0, 0])[1]

    def create_nodes(self, material_name):
        if super().create_nodes(material_name) in ['UNKNOWN', 'LOADED']:
            return

        material_output = self.create_node(Nodes.ShaderNodeOutputMaterial)
        shader = self.create_node(Nodes.ShaderNodeBsdfPrincipled, self.SHADER)
        self.connect_nodes(shader.outputs['BSDF'], material_output.inputs['Surface'])
        shader.inputs['Roughness'].default_value = self.roughness
        shader.inputs['Specular'].default_value = self.specular
        color_texture = self.color_texture
        normal_texture, roughness_texture = self.normal_texture

        albedo_node = self.create_node(Nodes.ShaderNodeTexImage, 'albedo')
        albedo_node.image = color_texture

        if self.color[0] != 1.0 and self.color[1] != 1.0 and self.color[2] != 1.0:
            color_mix = self.create_node(Nodes.ShaderNodeMixRGB)
            color_mix.blend_type = 'MULTIPLY'
            self.connect_nodes(albedo_node.outputs['Color'], color_mix.inputs['Color1'])
            color = self.color
            if sum(color) > 3:
                color = list(np.divide(color, 255))
            color_mix.inputs['Color2'].default_value = color
            color_mix.inputs['Fac'].default_value = 1.0
            self.connect_nodes(color_mix.outputs['Color'], shader.inputs['Base Color'])
        else:
            self.connect_nodes(albedo_node.outputs['Color'], shader.inputs['Base Color'])

        if self.translucent or self.alpha_test:
            self.bpy_material.blend_method = 'HASHED'
            self.bpy_material.shadow_method = 'HASHED'
            self.connect_nodes(albedo_node.outputs['Alpha'], shader.inputs['Alpha'])
        elif self.metalness:
            self.connect_nodes(albedo_node.outputs['Alpha'], shader.inputs['Metallic'])

        normal_map_texture = self.create_node(Nodes.ShaderNodeTexImage, 'normal')
        normal_map_texture.image = normal_texture

        normalmap_node = self.create_node(Nodes.ShaderNodeNormalMap)

        self.connect_nodes(normal_map_texture.outputs['Color'], normalmap_node.inputs['Color'])
        self.connect_nodes(normalmap_node.outputs['Normal'], shader.inputs['Normal'])

        # if self.selfillum:
        #     selfillummask = self.selfillummask
        #     albedo_node = self.get_node('$basetexture')
        #     if selfillummask is not None:
        #         selfillummask_node = self.create_node(Nodes.ShaderNodeTexImage, '$selfillummask')
        #         selfillummask_node.image = selfillummask
        #         self.connect_nodes(selfillummask_node.outputs['Color'], shader.inputs['Emission Strength'])
        #
        #     else:
        #         self.connect_nodes(albedo_node.outputs['Alpha'], shader.inputs['Emission Strength'])
        #     self.connect_nodes(albedo_node.outputs['Color'], shader.inputs['Emission'])
