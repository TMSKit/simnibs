import itertools
import json
import os
import re
import shutil
from copy import deepcopy
from typing import Callable, Optional

import jsonschema
import nibabel as nib
import numpy as np
import numpy.typing as npt
import scipy.optimize as opt

from simnibs import __version__
from simnibs.mesh_tools import mesh_io
from simnibs.mesh_tools.gmsh_view import Visualization, _gray_red_lightblue_blue_cm
from simnibs.mesh_tools.mesh_io import Elements, Msh, NodeData, Nodes
from simnibs.simulation.region_of_interest import RegionOfInterest
from simnibs.simulation.tms_coil.tcd_element import TcdElement
from simnibs.simulation.tms_coil.tms_coil_constants import TmsCoilElementTag
from simnibs.simulation.tms_coil.tms_coil_deformation import (
    TmsCoilDeformation,
    TmsCoilDeformationRange,
    TmsCoilTranslation,
    TmsCoilRotation,
)
from simnibs.simulation.tms_coil.tms_coil_element import (
    DipoleElements,
    LineSegmentElements,
    PositionalTmsCoilElements,
    SampledGridPointElements,
    TmsCoilElements,
)
from simnibs.simulation.tms_coil.tms_coil_model import TmsCoilModel
from simnibs.simulation.tms_coil.tms_stimulator import TmsStimulator, TmsWaveform
from simnibs.utils import file_finder
from simnibs.utils.mesh_element_properties import ElementTags


class TmsCoil(TcdElement):
    """A representation of a coil used for TMS

    Parameters
    ----------
    elements : list[TmsCoilElements]
        The stimulation elements of the coil
    name : Optional[str], optional
        The name of the coil, by default None
    brand : Optional[str], optional
        The brand of the coil, by default None
    version : Optional[str], optional
        The version of the coil, by default None
    limits : Optional[npt.ArrayLike] (3x2), optional
        Used for expansion into NIfTI digitized files, by default None.
        This is in mm and follows the structure [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]]
    resolution : Optional[npt.ArrayLike] (3), optional
        The sampling resolution (step width in mm) for expansion into NIfTI files, by default None.
        This follows the structure [rx,ry,rz]
    casing : Optional[TmsCoilModel], optional
        The casing of the coil, by default None
    self_intersection_test : Optional[list[list[TmsCoilElements]]], optional
        A list of lists of coil element indexes, each sublist describes a group of coil elements that should not be intersecting
        (0 is the coil casing, 1 is the first coil element)

    Attributes
    ----------------------
    elements : list[TmsCoilElements]
        The stimulation elements of the coil
    name : Optional[str]
        The name of the coil
    brand : Optional[str]
        The brand of the coil
    version : Optional[str]
        The version of the coil
    limits : Optional[npt.NDArray[np.float64]] (3x2)
        Used for expansion into NIfTI digitized files.
        This is in mm and follows the structure [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]]
    resolution : Optional[npt.NDArray[np.float64]] (3)
        The sampling resolution (step width in mm) for expansion into NIfTI files.
        This follows the structure [rx,ry,rz]
    casing : Optional[TmsCoilModel]
        The casing of the coil
    deformations : list[TmsCoilDeformation]
        All deformations used in the stimulation elements of the coil
    self_intersection_test : Optional[list[list[TmsCoilElements]]], optional
        A list of lists of coil elements, each sublist describes a group of coil elements that should not be intersecting
    """

    def __init__(
        self,
        elements: list[TmsCoilElements],
        name: Optional[str] = None,
        brand: Optional[str] = None,
        version: Optional[str] = None,
        limits: Optional[npt.ArrayLike] = None,
        resolution: Optional[npt.ArrayLike] = None,
        casing: Optional[TmsCoilModel] = None,
        self_intersection_test: Optional[list[list[int]]] = None,
    ):
        self.name = name
        self.brand = brand
        self.version = version
        self.limits = None if limits is None else np.array(limits, dtype=np.float64)
        self.resolution = (
            None if resolution is None else np.array(resolution, dtype=np.float64)
        )
        self.casing = casing
        self.elements = elements

        self.self_intersection_test = (
            [] if self_intersection_test is None else self_intersection_test
        )

        if len(elements) == 0:
            raise ValueError("Expected at least one coil element but got 0")

        if self.limits is not None and (
            self.limits.ndim != 2
            or self.limits.shape[0] != 3
            or self.limits.shape[1] != 2
        ):
            raise ValueError(
                f"Expected 'limits' to be in the format [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]] but shape was {self.limits.shape} ({self.limits})"
            )
        elif self.limits is not None and (
            self.limits[0, 0] >= self.limits[0, 1]
            or self.limits[1, 0] >= self.limits[1, 1]
            or self.limits[2, 0] >= self.limits[2, 1]
        ):
            raise ValueError(
                f"Expected 'limits' to be in the format [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]] but min was greater or equals than max ({self.limits})"
            )

        if self.resolution is not None and (
            self.resolution.ndim != 1 or self.resolution.shape[0] != 3
        ):
            raise ValueError(
                f"Expected 'resolution' to be in the format [rx,ry,rz] but shape was {self.resolution.shape} ({self.resolution})"
            )
        elif self.resolution is not None and (
            self.resolution[0] <= 0
            or self.resolution[1] <= 0
            or self.resolution[2] <= 0
        ):
            raise ValueError(
                f"Expected 'resolution' to have values greater than 0 ({self.resolution})"
            )

        for self_intersection_group in self.self_intersection_test:
            if len(self_intersection_group) != len(set(self_intersection_group)):
                raise ValueError(
                    f"Expected 'self_intersection_test' to have groups of unique indexes, but {self_intersection_group} has duplicates"
                )
            if len(self_intersection_group) <= 1:
                raise ValueError(
                    f"Expected 'self_intersection_test' to have groups of at least 2 coil element indexes, but {self_intersection_group} has less"
                )

    def get_deformation_ranges(self) -> list[TmsCoilDeformationRange]:
        """Returns all deformation ranges of all coil elements of this coil

        Returns
        -------
        list[TmsCoilDeformationRange]
            All deformation ranges of all coil elements of this coil
        """
        coil_deformation_ranges = []
        for coil_element in self.elements:
            for coil_deformation in coil_element.deformations:
                if coil_deformation.deformation_range not in coil_deformation_ranges:
                    coil_deformation_ranges.append(coil_deformation.deformation_range)

        return coil_deformation_ranges

    def get_deformations(self) -> list[TmsCoilDeformation]:
        """Returns all deformations of all coil elements of this coil

        Returns
        -------
        list[TmsCoilDeformation]
            All deformations of all coil elements of this coil
        """
        coil_deformations = []
        for coil_element in self.elements:
            for coil_deformation in coil_element.deformations:
                if coil_deformation not in coil_deformations:
                    coil_deformations.append(coil_deformation)

        return coil_deformations

    def get_da_dt(
        self,
        msh: Msh,
        coil_affine: npt.NDArray[np.float_],
        eps: float = 1e-3,
    ) -> NodeData:
        """Calculate the dA/dt field applied by the coil at each node of the mesh.
        The dI/dt value used for the simulation is set by the stimulators.

        Parameters
        ----------
        msh : Msh
            The mesh at which nodes the dA/dt field should be calculated
        coil_affine : npt.NDArray[np.float_]
            The affine transformation that is applied to the coil
        eps : float, optional
            The requested precision, by default 1e-3

        Returns
        -------
        NodeData
            The dA/dt field in V/m at every node of the mesh
        """
        target_positions = msh.nodes.node_coord
        A = self.get_da_dt_at_coordinates(target_positions, coil_affine, eps)

        node_data_result = NodeData(A)
        node_data_result.mesh = msh

        return node_data_result

    def get_da_dt_at_coordinates(
        self,
        coordinates: npt.NDArray[np.float_],
        coil_affine: npt.NDArray[np.float_],
        eps: float = 1e-3,
    ) -> npt.NDArray[np.float_]:
        """Calculate the dA/dt field applied by the coil at each node of the mesh.
        The dI/dt value used for the simulation is set by the stimulators.

        Parameters
        ----------
        coordinates : npt.NDArray[np.float_] (N x 3)
            The coordinates at which the dA/dt field should be calculated
        coil_affine : npt.NDArray[np.float_]
            The affine transformation that is applied to the coil
        eps : float, optional
            The requested precision, by default 1e-3

        Returns
        -------
        NodeData
            The dA/dt field in V/m at every coordinate
        """
        A = np.zeros_like(coordinates)
        for coil_element in self.elements:
            A += coil_element.get_da_dt(coordinates, coil_affine, eps)

        return A

    def get_a_field(
        self,
        points: npt.NDArray[np.float_],
        coil_affine: npt.NDArray[np.float_],
        eps: float = 1e-3,
    ) -> npt.NDArray[np.float_]:
        """Calculates the A field applied by the coil at each point.

        Parameters
        ----------
        points : npt.NDArray[np.float_] (N x 3)
            The points at which the A field should be calculated in mm
        coil_affine : npt.NDArray[np.float_] (4 x 4)
            The affine transformation that is applied to the coil
        eps : float, optional
            The requested precision, by default 1e-3

        Returns
        -------
        npt.NDArray[np.float_] (N x 3)
            The A field at every point in Tesla*meter
        """
        a_field = np.zeros_like(points)
        for coil_element in self.elements:
            a_field += coil_element.get_a_field(points, coil_affine, eps)

        return a_field

    def get_mesh(
        self,
        coil_affine: Optional[npt.NDArray[np.float_]] = None,
        apply_deformation: bool = True,
        include_casing: bool = True,
        include_optimization_points: bool = True,
        include_coil_elements: bool = True,
    ) -> Msh:
        """Generates a mesh of the coil

        Parameters
        ----------
        coil_affine : Optional[npt.NDArray[np.float_]], optional
            The affine transformation that is applied to the coil, by default None
        apply_deformation : bool, optional
            Whether or not to apply the current coil element deformations, by default True
        include_casing : bool, optional
            Whether or not to include the casing mesh, by default True
        include_optimization_points : bool, optional
            Whether or not to include the min distance, by default True
        include_coil_elements : bool, optional
            Whether or not to include the stimulating elements in the mesh, by default True

        Returns
        -------
        Msh
            The generated mesh of the coil
        """
        if coil_affine is None:
            coil_affine = np.eye(4)

        coil_msh = Msh()
        if self.casing is not None:
            coil_msh = coil_msh.join_mesh(
                self.casing.get_mesh(
                    coil_affine, include_casing, include_optimization_points, 0
                )
            )
        for i, coil_element in enumerate(self.elements):
            coil_msh = coil_msh.join_mesh(
                coil_element.get_mesh(
                    coil_affine,
                    apply_deformation,
                    include_casing,
                    include_optimization_points,
                    include_coil_elements,
                    (i + 1),
                )
            )
        return coil_msh

    def write_visualization(
        self, folder_path: str, base_file_name: str, apply_deformations=False
    ):
        visualization = Visualization(
            self.get_mesh(
                include_casing=False,
                apply_deformation=apply_deformations,
                include_optimization_points=False,
            )
        )
        casings = self.get_mesh(
            apply_deformation=apply_deformations,
            include_optimization_points=False,
            include_coil_elements=False,
        )
        optimization_points = self.get_mesh(
            apply_deformation=apply_deformations,
            include_casing=False,
            include_coil_elements=False,
        )

        visualization.visibility = np.unique(visualization.mesh.elm.tag1)

        for i, key in enumerate(visualization.mesh.field.keys()):
            if isinstance(self.elements[i], DipoleElements):
                visualization.add_view(Visible=1, VectorType=2, CenterGlyphs=0)
            elif isinstance(self.elements[i], LineSegmentElements):
                vector_lengths = np.linalg.norm(self.elements[i].values, axis=1)
                visualization.add_view(
                    Visible=1,
                    VectorType=2,
                    RangeType=2,
                    ShowScale=0,
                    CenterGlyphs=0,
                    GlyphLocation=2,
                    CustomMin=np.min(vector_lengths),
                    CustomMax=np.max(vector_lengths),
                    ArrowSizeMax=30,
                    ArrowSizeMin=30,
                )
                visualization.Mesh.PointSize = 2
            elif isinstance(self.elements[i], SampledGridPointElements):
                vector_lengths = np.linalg.norm(
                    visualization.mesh.field[key].value, axis=1
                )
                visualization.add_view(
                    Visible=1,
                    VectorType=1,
                    RangeType=2,
                    CenterGlyphs=0,
                    CustomMin=np.nanpercentile(vector_lengths, 99),
                    CustomMax=np.nanpercentile(vector_lengths, 99.99),
                    ArrowSizeMax=30,
                    ArrowSizeMin=10,
                )

        coords = visualization.mesh.nodes.node_coord
        if casings.elm.nr > 0:
            coords = np.concatenate((coords, casings.nodes.node_coord))
        if optimization_points.elm.nr > 0:
            coords = np.concatenate((coords, optimization_points.nodes.node_coord))
        min_coords = np.min(coords, axis=0)
        max_coords = np.max(coords, axis=0)
        bounding = Msh(
            Nodes(
                np.array(
                    [
                        min_coords,
                        [min_coords[0], min_coords[1], max_coords[2]],
                        [min_coords[0], max_coords[1], min_coords[2]],
                        [max_coords[0], min_coords[1], min_coords[2]],
                    ]
                )
            ),
            Elements(lines=np.array([[1, 2], [1, 3], [1, 4]])),
        )
        bounding.elm.tag1[:] = TmsCoilElementTag.BOUNDING_BOX
        bounding.elm.tag2[:] = TmsCoilElementTag.BOUNDING_BOX
        visualization.mesh = visualization.mesh.join_mesh(bounding)

        geo_file_name = os.path.join(folder_path, f"{base_file_name}.geo")
        if os.path.isfile(geo_file_name):
            os.remove(geo_file_name)

        for tag in np.unique(optimization_points.elm.tag1):
            element_optimization_points = optimization_points.crop_mesh(tags=[tag])
            index = str(tag)[:-2]
            identifier, color = ("min_distance_points", 2)

            mesh_io.write_geo_spheres(
                element_optimization_points.nodes.node_coord,
                geo_file_name,
                np.full((element_optimization_points.nodes.node_coord.shape[0]), 1),
                name=f"{index}-{identifier}",
                mode="ba",
            )
            visualization.add_view(
                ShowScale=0, PointType=1, PointSize=3.0, ColormapNumber=color
            )

        if not apply_deformations:
            for i, deformation in enumerate(self.get_deformations()):
                if isinstance(deformation, TmsCoilRotation):
                    mesh_io.write_geo_lines(
                        [deformation.point_1],
                        [deformation.point_2],
                        geo_file_name,
                        [[1, 1]],
                        name=f"{i}-rotation_axis",
                        mode="ba",
                    )
                    visualization.add_view(ShowScale=0, LineWidth=2)

        for tag in np.unique(casings.elm.tag1):
            casing = casings.crop_mesh(tags=[tag])
            index = str(tag)[:-2]
            index = index if len(index) > 0 else "0"
            mesh_io.write_geo_triangles(
                casing.elm.node_number_list - 1,
                casing.nodes.node_coord,
                geo_file_name,
                name=f"{index}-casing",
                mode="ba",
            )
            visualization.add_view(
                ColorTable=_gray_red_lightblue_blue_cm(),
                Visible=1,
                ShowScale=0,
                CustomMin=-0.5,
                CustomMax=3.5,
                RangeType=2,
            )

        if optimization_points.elm.nr > 0 or casings.elm.nr > 0:
            visualization.add_merge(geo_file_name)
        visualization.mesh.write(os.path.join(folder_path, f"{base_file_name}.msh"))
        visualization.write_opt(os.path.join(folder_path, f"{base_file_name}.msh"))

    def append_simulation_visualization(
        self,
        visualization: Visualization,
        goe_fn: str,
        msh_skin: Msh,
        coil_matrix: npt.NDArray[np.float_],
    ):
        for i, element in enumerate(self.elements):
            points = []
            vectors = []
            if isinstance(element, DipoleElements):
                points = element.get_points(coil_matrix)
                vectors = element.get_values(coil_matrix)
                visualization.add_view(
                    Visible=1, VectorType=2, CenterGlyphs=0, ShowScale=0
                )
                mesh_io.write_geo_vectors(
                    points, vectors, goe_fn, name=f"{i+1}-dipoles", mode="ba"
                )
            elif isinstance(element, LineSegmentElements):
                points = element.get_points(coil_matrix)
                vectors = element.get_values(coil_matrix)
                vector_lengths = np.linalg.norm(vectors, axis=1)
                visualization.add_view(
                    VectorType=2,
                    RangeType=2,
                    CenterGlyphs=0,
                    GlyphLocation=2,
                    ShowScale=0,
                    CustomMin=np.min(vector_lengths),
                    CustomMax=np.max(vector_lengths),
                    ArrowSizeMax=30,
                    ArrowSizeMin=30,
                )
                mesh_io.write_geo_vectors(
                    points, vectors, goe_fn, name=f"{i+1}-line_segments", mode="ba"
                )
            elif isinstance(element, SampledGridPointElements):
                y_axis = np.arange(1, 10, dtype=float)[:, None] * (0, 1, 0)
                z_axis = np.arange(1, 30, dtype=float)[:, None] * (0, 0, 1)
                pos = np.vstack((((0, 0, 0)), y_axis, z_axis))
                pos = (coil_matrix[:3, :3].dot(pos.T) + coil_matrix[:3, 3][:, None]).T
                directions = pos[1:] - pos[:-1]
                directions = np.vstack((directions, np.zeros(3)))
                points = pos
                vectors = directions
                visualization.add_view(ShowScale=0)
                mesh_io.write_geo_vectors(
                    points,
                    vectors,
                    goe_fn,
                    name=f"{i+1}-sampled_grid_points",
                    mode="ba",
                )

        casings = self.get_mesh(
            coil_affine=coil_matrix,
            apply_deformation=True,
            include_optimization_points=False,
            include_coil_elements=False,
        )
        if casings.nodes.nr != 0:
            index_offset = 0 if self.casing is not None else 1
            for i, tag in enumerate(np.unique(casings.elm.tag1)):
                casing = casings.crop_mesh(tags=[tag])

                idx_inside = msh_skin.pts_inside_surface(casing.nodes[:])
                casing.elm.tag1[:] = 0
                if len(idx_inside):
                    idx_hlp = np.zeros((casing.nodes.nr, 1), dtype=bool)
                    idx_hlp[idx_inside] = True
                    idx_hlp = np.any(np.squeeze(idx_hlp[casing.elm[:, :3] - 1]), axis=1)
                    casing.elm.tag1[idx_hlp & (casing.elm.tag1 == 0)] = 1

                mesh_io.write_geo_triangles(
                    casing.elm.node_number_list - 1,
                    casing.nodes.node_coord,
                    goe_fn,
                    values=casing.elm.tag1,
                    name=f"{i+index_offset}-coil_casing",
                    mode="ba",
                )
                visualization.add_view(
                    ColorTable=_gray_red_lightblue_blue_cm(),
                    Visible=1,
                    ShowScale=0,
                    CustomMin=-0.5,
                    CustomMax=3.5,
                    RangeType=2,
                )

    def get_casing_coordinates(
        self,
        affine: Optional[npt.NDArray[np.float_]] = None,
        apply_deformation: bool = True,
    ) -> tuple[npt.NDArray[np.float_], npt.NDArray[np.float_]]:
        """Returns all casing points and min distance points of this coil and the coil elements.

        Parameters
        ----------
        affine : Optional[npt.NDArray[np.float_]], optional
            The affine transformation that is applied to the coil, by default None
        apply_deformation : bool, optional
            Whether or not to apply the current coil element deformations, by default True

        Returns
        -------
        tuple[npt.NDArray[np.float_], npt.NDArray[np.float_]
            A tuple containing the casing points and min distance points
        """
        if affine is None:
            affine = np.eye(4)

        casing_points = (
            [self.casing.get_points(affine)] if self.casing is not None else []
        )
        min_distance_points = (
            [self.casing.get_min_distance_points(affine)]
            if self.casing is not None and len(self.casing.min_distance_points) > 0
            else []
        )

        for coil_element in self.elements:
            if coil_element.casing is not None:
                element_casing_points = coil_element.get_casing_coordinates(
                    affine, apply_deformation
                )
                if len(element_casing_points[0]) > 0:
                    casing_points.append(element_casing_points[0])
                if len(element_casing_points[1]) > 0:
                    min_distance_points.append(element_casing_points[1])

        if len(casing_points) > 0:
            casing_points = np.concatenate(casing_points, axis=0)
        if len(min_distance_points) > 0:
            min_distance_points = np.concatenate(min_distance_points, axis=0)

        return casing_points, min_distance_points

    def get_elements_grouped_by_stimulators(
        self,
    ) -> dict[TmsStimulator, list[TmsCoilElements]]:
        """Returns a dictionary mapping from each stimulator to the list of elements using it

        Returns
        -------
        dict[TmsStimulator, list[TmsCoilElements]]
            The dictionary mapping from each stimulator to the list of elements using it
        """
        stimulators = []
        elements_by_stimulators = {}

        for element in self.elements:
            if element.stimulator is None:
                raise ValueError(
                    "Every coil element needs to have a stimulator attached!"
                )
            if element.stimulator in stimulators:
                elements_by_stimulators[element.stimulator].append(element)
                continue
            stimulators.append(element.stimulator)
            elements_by_stimulators[element.stimulator] = [element]

        return elements_by_stimulators

    @staticmethod
    def _add_logo(mesh: Msh) -> Msh:
        """Adds the SimNIBS logo to the coil surface

        Parameters
        ----------
        mesh : Msh
            The mesh of the coil

        Returns
        -------
        Msh
            The coil mesh including the SimNIBS logo
        """

        msh_logo = Msh(fn=file_finder.templates.simnibs_logo)

        # 'simnibs' has tag 1, '3' has tag 2, '4' has tag 3
        # renumber tags, because they will be converted to color:
        # 0 gray, 1 red, 2 lightblue, 3 blue
        major_version = __version__.split(".")[0]
        if major_version == "3":
            msh_logo = msh_logo.crop_mesh(tags=[1, 2])
            msh_logo.elm.tag1[msh_logo.elm.tag1 == 2] = 3  # version in blue
        elif major_version == "4":
            msh_logo = msh_logo.crop_mesh(tags=[1, 3])
        else:
            msh_logo = msh_logo.crop_mesh(tags=1)
        msh_logo.elm.tag1[msh_logo.elm.tag1 == 1] = 2  # 'simnibs' in light blue

        # center logo in xy-plane, mirror at yz-plane and scale
        bbox_coil = np.vstack([np.min(mesh.nodes[:], 0), np.max(mesh.nodes[:], 0)])
        bbox_logo = np.vstack(
            [np.min(msh_logo.nodes[:], 0), np.max(msh_logo.nodes[:], 0)]
        )
        bbox_ratio = np.squeeze(np.diff(bbox_logo, axis=0) / np.diff(bbox_coil, axis=0))
        bbox_ratio = max(bbox_ratio[0:2])  # maximal size ratio in xy plane

        msh_logo.nodes.node_coord[:, 0:2] -= np.mean(bbox_logo[:, 0:2], axis=0)
        msh_logo.nodes.node_coord[:, 0] = -msh_logo.nodes.node_coord[:, 0]
        msh_logo.nodes.node_coord[:, 0:2] *= 1 / (4 * bbox_ratio)

        # shift logo along negative z to the top side of coil
        msh_logo.nodes.node_coord[:, 2] += bbox_coil[0, 2] - bbox_logo[0, 2] - 5

        mesh = mesh.join_mesh(msh_logo)
        return mesh

    @classmethod
    def from_file(cls, fn: str):
        """Loads the coil file. The file has to be either in the tcd, ccd or the NIfTI format

        Parameters
        ----------
        fn : str
            The path to the coil file

        Returns
        -------
        TmsCoil
            The tms coil loaded from the coil file

        Raises
        ------
        IOError
            If the file type is unsupported or the file extension for a NIfTI file is missing
        """
        if fn.endswith(".tcd"):
            return TmsCoil.from_tcd(fn)
        elif fn.endswith(".ccd"):
            return TmsCoil.from_ccd(fn)
        elif fn.endswith(".nii.gz") or fn.endswith(".nii"):
            return TmsCoil.from_nifti(fn)

        try:
            return TmsCoil.from_tcd(fn)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        try:
            return TmsCoil.from_ccd(fn)
        except (UnicodeDecodeError, ValueError):
            pass

        try:
            shutil.copy2(
                fn, os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii.gz")
            )
            coil = TmsCoil.from_nifti(
                os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii.gz")
            )
            return coil
        except nib.filebasedimages.ImageFileError:
            pass
        finally:
            if os.path.exists(
                os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii.gz")
            ):
                os.remove(
                    os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii.gz")
                )

        try:
            shutil.copy2(
                fn, os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii")
            )
            coil = TmsCoil.from_nifti(
                os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii")
            )
            return coil
        except nib.filebasedimages.ImageFileError:
            pass
        finally:
            if os.path.exists(
                os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii")
            ):
                os.remove(
                    os.path.join(os.path.dirname(fn), f"{os.path.basename(fn)}.nii")
                )

        raise IOError(f"Error loading file: Unsupported file type '{fn}'")

    def write(self, fn: str, ascii_mode: bool = False):
        """Writes the TMS coil in the tcd format

        Parameters
        ----------
        fn : str
            The path and file name to store the tcd coil file as
        ascii_mode : bool, optional
            Whether or not to write arrays in an ascii format, by default False
        """
        self.write_tcd(fn, ascii_mode)

    @classmethod
    def from_ccd(
        cls,
        fn: str,
        fn_coil_casing: Optional[str] = None,
        fn_waveform_file: Optional[str] = None,
    ):
        """Loads a ccd coil file with the optional addition of a coil casing as an stl file and waveform information from a tsv file
        If the additional files are None, files with the same name as the coil file are tried to be loaded.

        Parameters
        ----------
        fn : str
            The path to the ccd coil file
        fn_coil_casing : Optional[str], optional
            The path to a stl coil casing file, by default None
        fn_waveform_file : Optional[str], optional
            the path to a tsv waveform information file, by default None

        Returns
        -------
        TmsCoil
            The coil loaded from the ccd (and optional stl and tsv) file
        """
        with open(fn, "r") as f:
            header = f.readline()

        if fn_coil_casing is None:
            fn_coil_casing = f"{os.path.splitext(fn)[0]}.stl"
            if not os.path.exists(fn_coil_casing):
                fn_coil_casing = None

        coil_casing = None
        if fn_coil_casing is not None:
            coil_casing_mesh = mesh_io.read_stl(fn_coil_casing)
            coil_casing = TmsCoilModel(coil_casing_mesh, None)

        if fn_waveform_file is None:
            fn_waveform_file = f"{os.path.splitext(fn)[0]}.tsv"
            if not os.path.exists(fn_waveform_file):
                fn_waveform_file = None

        waveforms = None
        if fn_waveform_file is not None:
            waveform_data = np.genfromtxt(
                fn_waveform_file, delimiter="\t", filling_values=0, names=True
            )
            names = waveform_data.dtype.names
            waveforms = [
                TmsWaveform(
                    waveform_data[names[0]],
                    waveform_data[names[1]],
                    names[1],
                    waveform_data[names[2]],
                )
            ]

        meta_informations = header.replace("\n", "").split(";")
        file_discription = meta_informations[0]
        version_match = re.search(r"version (\d+\.\d+)", file_discription)
        file_version = version_match.group(1) if version_match else None

        parametric_information = meta_informations[1:]
        parametric_information = [pair.strip() for pair in parametric_information]
        parametric_information = [
            pair for pair in parametric_information if len(pair) > 0
        ]

        header_dict = {}
        for pair in parametric_information:
            key, value = pair.split("=")

            if value == "none":
                value = None
            elif "." in value:
                value = float(value)
            elif value.isdigit():
                value = int(value)
            elif "," in value:
                value = np.fromstring(value, dtype=int, sep=",")

            header_dict[key] = value

        bb = []
        for dim in ("x", "y", "z"):
            a = header_dict.get(dim)
            if a is not None:
                if len(a) < 2:
                    bb.append((-np.abs(a[0]), np.abs(a[0])))
                else:
                    bb.append(a)

        if len(bb) != 3:
            bb = None

        res = []
        a = header_dict.get("resolution")
        if a is not None:
            a = np.atleast_1d(a)
            if len(a) < 3:
                for i in range(len(a), 3):
                    a = np.concatenate((a, (a[i - 1],)))
            res = a

        if len(res) != 3:
            res = None

        ccd_file = np.atleast_2d(np.loadtxt(fn, skiprows=2))

        dipole_positions = ccd_file[:, 0:3] * 1e3
        dipole_moments = ccd_file[:, 3:]

        stimulator = TmsStimulator(
            header_dict.get("stimulator"),
            max_di_dt=None
            if header_dict.get("dIdtmax") is None
            else header_dict["dIdtmax"] * 1e6,
            waveforms=waveforms,
        )

        coil_elements = [DipoleElements(stimulator, dipole_positions, dipole_moments)]

        return cls(
            coil_elements,
            header_dict.get("coilname"),
            header_dict.get("brand"),
            file_version,
            None if bb is None else np.array(bb),
            None if res is None else np.array(res),
            coil_casing,
        )

    def to_tcd(self, ascii_mode: bool = False) -> dict:
        """Packs the coil information into a tcd like dictionary

        Parameters
        ----------
        ascii_mode : bool, optional
            Whether or not to write arrays in an ascii format, by default False

        Returns
        -------
        dict
            A tcd like dictionary representing the coil
        """

        tcd_coil_models = []
        coil_models = []
        if self.casing is not None:
            tcd_coil_models.append(self.casing.to_tcd(ascii_mode))
            coil_models.append(self.casing)

        tcd_deform_ranges = []
        tcd_deforms = []

        tcd_stimulators = []
        stimulators = []

        deformations = []
        deformation_ranges = []
        for coil_element in self.elements:
            for coil_deformation in coil_element.deformations:
                if coil_deformation not in deformations:
                    deformations.append(coil_deformation)
                if coil_deformation.deformation_range not in deformation_ranges:
                    deformation_ranges.append(coil_deformation.deformation_range)

        for deformation_range in deformation_ranges:
            tcd_deform_ranges.append(deformation_range.to_tcd(ascii_mode))
        for deformation in deformations:
            tcd_deforms.append(deformation.to_tcd(deformation_ranges, ascii_mode))

        tcd_coil_elements = []
        for coil_element in self.elements:
            if (
                coil_element.casing not in coil_models
                and coil_element.casing is not None
            ):
                coil_models.append(coil_element.casing)
                tcd_coil_models.append(coil_element.casing.to_tcd(ascii_mode))

            if (
                coil_element.stimulator not in stimulators
                and coil_element.stimulator is not None
                and (
                    coil_element.stimulator.name is not None
                    or coil_element.stimulator.brand is not None
                    or coil_element.stimulator.max_di_dt is not None
                    or len(coil_element.stimulator.waveforms) > 0
                )
            ):
                stimulators.append(coil_element.stimulator)
                tcd_stimulators.append(coil_element.stimulator.to_tcd(ascii_mode))

            tcd_coil_elements.append(
                coil_element.to_tcd(stimulators, coil_models, deformations, ascii_mode)
            )

        tcd_coil = {}
        if self.name is not None:
            tcd_coil["name"] = self.name
        if self.brand is not None:
            tcd_coil["brand"] = self.brand
        if self.version is not None:
            tcd_coil["version"] = self.version
        if self.limits is not None:
            tcd_coil["limits"] = self.limits.tolist()
        if self.resolution is not None:
            tcd_coil["resolution"] = self.resolution.tolist()
        if self.casing is not None:
            tcd_coil["coilCasing"] = coil_models.index(self.casing)
        tcd_coil["coilElementList"] = tcd_coil_elements
        if len(tcd_stimulators) > 0:
            tcd_coil["stimulatorList"] = tcd_stimulators
        if len(tcd_deforms) > 0:
            tcd_coil["deformList"] = tcd_deforms
        if len(tcd_deform_ranges) > 0:
            tcd_coil["deformRangeList"] = tcd_deform_ranges
        if len(tcd_coil_models) > 0:
            tcd_coil["coilModels"] = tcd_coil_models
        if len(self.self_intersection_test) > 0:
            tcd_coil["selfIntersectionTest"] = self.self_intersection_test

        return tcd_coil

    @classmethod
    def from_tcd_dict(cls, coil: dict, validate=True):
        """Loads the coil from a tcd like dictionary

        Parameters
        ----------
        coil : dict
            A tcd like dictionary storing coil information
        validate : bool, optional
            Whether or not to validate the dictionary based on the tcd coil json schema, by default True

        Returns
        -------
        TmsCoil
            The TMS coil loaded from the tcd like dictionary

        Raises
        ------
        ValidationError
            Raised if validate is true and the dictionary is not valid to the tcd coil json schema
        """
        if validate:
            with open(file_finder.templates.tcd_json_schema, "r") as fid:
                tcd_schema = json.loads(fid.read())

            try:
                jsonschema.validate(coil, tcd_schema)
            except jsonschema.ValidationError as e:
                instance = str(e.instance)
                e.instance = (
                    instance
                    if len(instance) < 900
                    else f"{instance[:400]} ... {instance[-400:]}"
                )
                raise e

        coil_models = []
        for coil_model in coil.get("coilModels", []):
            coil_models.append(TmsCoilModel.from_tcd_dict(coil_model))

        deformation_ranges = []
        for deform_range in coil.get("deformRangeList", []):
            deformation_ranges.append(TmsCoilDeformationRange.from_tcd(deform_range))

        deformations = []
        for deform in coil.get("deformList", []):
            deformations.append(TmsCoilDeformation.from_tcd(deform, deformation_ranges))

        stimulators = []
        for stimulator in coil.get("stimulatorList", []):
            stimulators.append(TmsStimulator.from_tcd(stimulator))

        coil_elements = []
        for coil_element in coil["coilElementList"]:
            coil_elements.append(
                TmsCoilElements.from_tcd_dict(
                    coil_element,
                    stimulators,
                    coil_models,
                    deformations,
                )
            )

        coil_casing = (
            None if coil.get("coilCasing") is None else coil_models[coil["coilCasing"]]
        )

        return cls(
            coil_elements,
            coil.get("name"),
            coil.get("brand"),
            coil.get("version"),
            None if coil.get("limits") is None else np.array(coil["limits"]),
            None if coil.get("resolution") is None else np.array(coil["resolution"]),
            coil_casing,
            coil.get("selfIntersectionTest", []),
        )

    @classmethod
    def from_tcd(cls, fn: str, validate=True):
        """Loads the TMS coil from a tcd file

        Parameters
        ----------
        fn : str
            The path to the ccd coil file
        validate : bool, optional
            Whether or not to validate the dictionary based on the tcd coil json schema, by default True

        Returns
        -------
        TmsCoil
            The TMS coil loaded from the tcd file
        """
        with open(fn, "r") as fid:
            coil = json.loads(fid.read())

        return cls.from_tcd_dict(coil, validate)

    def write_tcd(self, fn: str, ascii_mode: bool = False):
        """Writes the coil as a tcd file

        Parameters
        ----------
        fn : str
            The path and file name to store the tcd coil file as
        ascii_mode : bool, optional
            Whether or not to write arrays in an ascii format, by default False
        """

        with open(fn, "w") as json_file:
            json.dump(self.to_tcd(ascii_mode), json_file, indent=4)

    @classmethod
    def from_nifti(cls, fn: str, fn_coil_casing: Optional[str] = None):
        """Loads coil information from a NIfTI file with the optional addition of a coil casing as an stl file and waveform information from a tsv file
        If the additional files are None, files with the same name as the coil file are tried to be loaded.

        Parameters
        ----------
        fn : str
            The path to the coil NIfTI file
        fn_coil_casing : Optional[str], optional
            The path to a stl coil casing file, by default None

        Returns
        -------
        TmsCoil
            The TMS coil loaded from the NIfTI file
        """
        if fn_coil_casing is None:
            fn_coil_casing = f"{os.path.splitext(fn)[0]}.stl"
            if not os.path.exists(fn_coil_casing):
                fn_coil_casing = None

        coil_casing = None
        if fn_coil_casing is not None:
            coil_casing_mesh = mesh_io.read_stl(fn_coil_casing)
            coil_casing = TmsCoilModel(coil_casing_mesh, None)

        nifti = nib.load(fn)
        data = nifti.get_fdata()
        affine = nifti.affine

        resolution = np.array(
            [
                affine[0][0],
                affine[1][1],
                affine[2][2],
            ]
        )

        limits = np.array(
            [
                [affine[0][3], data.shape[0] * resolution[0] + affine[0][3]],
                [affine[1][3], data.shape[1] * resolution[1] + affine[1][3]],
                [affine[2][3], data.shape[2] * resolution[2] + affine[2][3]],
            ]
        )

        if len(data.shape) == 4:
            coil_elements = [
                SampledGridPointElements(TmsStimulator("Generic"), data, affine)
            ]
        elif len(data.shape) == 5 and data.shape[3] == 1:
            element_data = data.reshape(
                data.shape[0], data.shape[1], data.shape[2], data.shape[4]
            )
            coil_elements = [
                SampledGridPointElements(
                    TmsStimulator("Generic"),
                    element_data,
                    affine,
                )
            ]
        elif len(data.shape) == 5:
            data = np.split(data, data.shape[-2], axis=-2)
            coil_elements = []
            for i, element_data in enumerate(data):
                element_data = element_data.reshape(
                    element_data.shape[0],
                    element_data.shape[1],
                    element_data.shape[2],
                    element_data.shape[4],
                )
                coil_elements.append(
                    SampledGridPointElements(
                        TmsStimulator(f"Generic-{i + 1}"),
                        element_data,
                        affine,
                    )
                )
        else:
            raise ValueError(
                "NIfTI file needs to at least contain one 3D vector per voxel!"
            )

        return cls(
            coil_elements, limits=limits, resolution=resolution, casing=coil_casing
        )

    def write_nifti(
        self,
        fn: str,
        limits: Optional[npt.NDArray[np.float_]] = None,
        resolution: Optional[npt.NDArray[np.float_]] = None,
    ):
        """Writes the A field of the coil in the NIfTI file format.
        If multiple stimulators are present, the NIfTI file will be 5D and containing vector data for each stimulator group.

        Parameters
        ----------
        fn : str
           The path and file name to store the NIfTI coil file as
        limits : Optional[npt.NDArray[np.float_]], optional
            Overrides the limits set in the coil object, by default None
        resolution : Optional[npt.NDArray[np.float_]], optional
            Overrides the resolution set in the coil object, by default None

        Raises
        ------
        ValueError
            If the limits are not set in the coil object or as a parameter
        ValueError
            If the resolution is not set in the coil object or as a parameter
        """
        limits = limits if limits is not None else self.limits
        if limits is None:
            raise ValueError("Limits needs to be set")
        resolution = resolution if resolution is not None else self.resolution
        if resolution is None:
            raise ValueError("resolution needs to be set")

        stimulators_to_elements = self.get_elements_grouped_by_stimulators()
        sample_positions, dims = self.get_sample_positions(limits, resolution)

        data_per_stimulator = []
        for stimulator in stimulators_to_elements.keys():
            stimulator_a_field = np.zeros_like(sample_positions)
            for element in stimulators_to_elements[stimulator]:
                stimulator_a_field += element.get_a_field(sample_positions, np.eye(4))

            stimulator_a_field = stimulator_a_field.reshape(
                (dims[0], dims[1], dims[2], 3)
            )
            data_per_stimulator.append(stimulator_a_field)

        data = np.stack(data_per_stimulator, axis=-2)

        affine = np.array(
            [
                [resolution[0], 0, 0, limits[0][0]],
                [0, resolution[1], 0, limits[1][0]],
                [0, 0, resolution[2], limits[2][0]],
                [0, 0, 0, 1],
            ]
        )
        nib.save(nib.Nifti1Image(data, affine), fn)

    def get_sample_positions(
        self,
        limits: Optional[npt.NDArray[np.float_]] = None,
        resolution: Optional[npt.NDArray[np.float_]] = None,
    ) -> tuple[npt.NDArray[np.float_], list[int]]:
        """Returns the sampled positions and the dimensions calculated using the limits and resolution of this TMS coil or the parameter if supplied

        Parameters
        ----------
        limits : Optional[npt.NDArray[np.float_]], optional
            Overrides the limits set in the coil object, by default None
        resolution : Optional[npt.NDArray[np.float_]], optional
            Overrides the resolution set in the coil object, by default None

        Returns
        -------
        tuple[npt.NDArray[np.float_], list[int]]
            The sampled positions and the dimensions calculated using the limits and resolution of this TMS coil or the parameter if supplied

        Raises
        ------
         ValueError
            If the limits are not set in the coil object or as a parameter
        ValueError
            If the resolution is not set in the coil object or as a parameter
        """
        limits = limits if limits is not None else self.limits
        if limits is None:
            raise ValueError("Limits needs to be set")
        resolution = resolution if resolution is not None else self.resolution
        if resolution is None:
            raise ValueError("resolution needs to be set")

        dims = [
            int((max_ - min_) // res) for [min_, max_], res in zip(limits, resolution)
        ]

        dx = np.spacing(1e4)
        x = np.linspace(limits[0][0], limits[0][1] - resolution[0] + dx, dims[0])
        y = np.linspace(limits[1][0], limits[1][1] - resolution[1] + dx, dims[1])
        z = np.linspace(limits[2][0], limits[2][1] - resolution[2] + dx, dims[2])
        return np.array(np.meshgrid(x, y, z, indexing="ij")).reshape((3, -1)).T, dims

    def as_sampled(
        self,
        limits: Optional[npt.NDArray[np.float_]] = None,
        resolution: Optional[npt.NDArray[np.float_]] = None,
        resample_sampled_elements: bool = False,
    ) -> "TmsCoil":
        """Turns every coil element into SampledGridPointElements.
        If resample_sampled_elements is true, existing SampledGridPointElements are resampled.

        Parameters
        ----------
        limits : Optional[npt.NDArray[np.float_]], optional
            Overrides the limits set in the coil object, by default None
        resolution : Optional[npt.NDArray[np.float_]], optional
            Overrides the resolution set in the coil object, by default None
        resample_sampled_elements : bool, optional
             Whether or not to resample existing SampledGridPointElements, by default False

        Returns
        -------
        TmsCoil
            A new TMS coil where all elements are replaced by SampledGridPointElements.

        Raises
        ------
         ValueError
            If the limits are not set in the coil object or as a parameter
        ValueError
            If the resolution is not set in the coil object or as a parameter
        """
        limits = limits if limits is not None else self.limits
        if limits is None:
            raise ValueError("Limits needs to be set")
        resolution = resolution if resolution is not None else self.resolution
        if resolution is None:
            raise ValueError("resolution needs to be set")

        affine = np.array(
            [
                [resolution[0], 0, 0, limits[0][0]],
                [0, resolution[1], 0, limits[1][0]],
                [0, 0, resolution[2], limits[2][0]],
                [0, 0, 0, 1],
            ]
        )

        sample_positions, dims = self.get_sample_positions(limits, resolution)

        sampled_coil_elements = []
        for coil_element in self.elements:
            if (
                isinstance(coil_element, SampledGridPointElements)
                and not resample_sampled_elements
            ):
                sampled_coil_elements.append(coil_element)
            else:
                data = coil_element.get_a_field(
                    sample_positions, np.eye(4), apply_deformation=False
                ).reshape(((dims[0], dims[1], dims[2], 3)))

                sampled_coil_elements.append(
                    SampledGridPointElements(
                        coil_element.stimulator,
                        data,
                        affine,
                        coil_element.name,
                        coil_element.casing,
                        coil_element.deformations,
                    )
                )

        return deepcopy(
            TmsCoil(
                sampled_coil_elements,
                self.name,
                self.brand,
                self.version,
                limits,
                resolution,
                self.casing,
                self.self_intersection_test,
            )
        )

    def as_sampled_squashed(
        self,
        limits: Optional[npt.NDArray[np.float_]] = None,
        resolution: Optional[npt.NDArray[np.float_]] = None,
    ) -> "TmsCoil":
        """Turns the coil elements grouped by the stimulators into sampled elements and returns the resulting TMS coil.
        Deformations are applied before the sampling.

        Parameters
        ----------
        limits : Optional[npt.NDArray[np.float_]], optional
            Overrides the limits set in the coil object, by default None
        resolution : Optional[npt.NDArray[np.float_]], optional
            Overrides the resolution set in the coil object, by default None

        Returns
        -------
        TmsCoil
            The combined TMS coil containing one sampled element per stimulator

        Raises
        ------
         ValueError
            If the limits are not set in the coil object or as a parameter
        ValueError
            If the resolution is not set in the coil object or as a parameter
        """
        limits = limits if limits is not None else self.limits
        if limits is None:
            raise ValueError("Limits needs to be set")
        resolution = resolution if resolution is not None else self.resolution
        if resolution is None:
            raise ValueError("resolution needs to be set")

        affine = np.array(
            [
                [resolution[0], 0, 0, limits[0][0]],
                [0, resolution[1], 0, limits[1][0]],
                [0, 0, resolution[2], limits[2][0]],
                [0, 0, 0, 1],
            ]
        )

        sample_positions, dims = self.get_sample_positions(limits, resolution)
        stimulator_to_elements = self.get_elements_grouped_by_stimulators()

        sampled_coil_elements = []
        for stimulator in stimulator_to_elements.keys():
            combined_data = np.zeros_like(sample_positions)
            combined_casing = None
            for coil_element in stimulator_to_elements[stimulator]:
                combined_data += coil_element.get_a_field(
                    sample_positions, np.eye(4), apply_deformation=False
                )
                if combined_casing is None and coil_element.casing is not None:
                    combined_casing = coil_element.casing
                elif combined_casing is not None and coil_element.casing is not None:
                    combined_casing = combined_casing.merge(coil_element.casing)

            sampled_coil_elements.append(
                SampledGridPointElements(
                    stimulator,
                    combined_data.reshape(((dims[0], dims[1], dims[2], 3))),
                    affine,
                    casing=combined_casing,
                )
            )

        return deepcopy(
            TmsCoil(
                sampled_coil_elements,
                self.name,
                self.brand,
                self.version,
                limits,
                resolution,
                self.casing,
                self.self_intersection_test,
            )
        )

    def freeze_deformations(self) -> "TmsCoil":
        """Creates a new TMS coil without any coil deformations where the current deformations are applied.

        Returns
        -------
        TmsCoil
            A new TMS coil without any coil deformations where the current deformations are applied
        """
        frozen_elements = []
        for element in self.elements:
            frozen_element = element.freeze_deformations()
            frozen_elements.append(frozen_element)

        return deepcopy(
            TmsCoil(
                frozen_elements,
                self.name,
                self.brand,
                self.version,
                self.limits,
                self.resolution,
                self.casing,
                self.self_intersection_test,
            )
        )

    def generate_element_casings(
        self,
        distance: float,
        grid_spacing: float,
        override: bool = False,
        combined_casing: bool = False,
    ):
        """Generates coil element casings for all coil elements except sampled elements.
        The casing will have the specified distance from the coil element points.
        If override is true, all element casings will be overridden by the generated ones.

        Parameters
        ----------
        distance : float
            The minimum distance of the casing to the element points
        grid_spacing : float
            The spacing of the discretization grid used to generate the casing
        override : bool, optional
            Whether or not to override existing coil casings, by default False
        combined_casing : bool, optional
            Whether or not to combine the element casings into one coil casing, by default False
        """
        if combined_casing and (self.casing is None or override):
            all_points = []
            for element in self.elements:
                if isinstance(element, PositionalTmsCoilElements):
                    all_points.append(element.points)
            self.casing = TmsCoilModel.from_points(
                np.concatenate(all_points, axis=0),
                distance,
                grid_spacing,
            )
        else:
            for element in self.elements:
                if isinstance(element, PositionalTmsCoilElements) and (
                    element.casing is None or override
                ):
                    element.casing = TmsCoilModel.from_points(
                        element.points, distance, grid_spacing
                    )

    def _get_fast_distance_score(
        self,
        distance_function: Callable,
        elements: list[TmsCoilElements],
        affine: npt.NDArray[np.float_],
    ) -> float:
        """Calculates the mean absolute distance from the min distance points of the coil using the distance function.
        If no min distance points are present it will use the node positions of the casings.

        Parameters
        ----------
        distance_function : Callable
            A distance function calculating the distance between input points and a target
        elements : list[TmsCoilElements]
            The coil elements to be used
        affine : npt.NDArray[np.float_]
            The affine transformation from coil to world space

        Returns
        -------
        float
            The mean absolute distance from the min distance points (coil casing nodes) using the distance function
        """
        casing_points = []
        min_distance_points = []

        for coil_element in elements:
            if coil_element.casing is not None:
                element_casing_points = coil_element.get_casing_coordinates(
                    affine, True
                )
                if len(element_casing_points[0]) > 0:
                    casing_points.append(element_casing_points[0])
                if len(element_casing_points[1]) > 0:
                    min_distance_points.append(element_casing_points[1])

        if len(casing_points) > 0:
            casing_points = np.concatenate(casing_points, axis=0)
        if len(min_distance_points) > 0:
            min_distance_points = np.concatenate(min_distance_points, axis=0)

        min_distance_points = (
            min_distance_points if len(min_distance_points) > 0 else casing_points
        )

        return np.mean(np.abs(distance_function(min_distance_points)))

    def _get_fast_intersection_penalty(
        self,
        element_voxel_volumes: dict,
        element_voxel_indexes,
        element_voxel_affines: dict,
        target_voxel_distance,
        target_voxel_affine,
        self_intersection_elements,
        affine: npt.NDArray[np.float_],
    ) -> tuple[float, float]:
        """Evaluates how far the element voxel volumes intersect with the target voxel volume measured in mm^3 (volume) * mm (depth).
        Evaluates the amount of self intersection based on the self intersection element groups measured in mm^3.


        Parameters
        ----------
        element_voxel_volume : dict[TmsCoilElements, npt.NDArray[np.bool_]]
            The voxel volume (True inside, False outside) of each coil element
        element_voxel_indexes : dict[TmsCoilElements, npt.NDArray[np.int_]]
            The indexes of the inside voxels for each coil element
        element_voxel_affine : dict[TmsCoilElements, npt.NDArray[np.float_]]
            The affine transformations from world to voxel space for each coil element
        target_voxel_distance : _type_
            The voxel distance field of the target
        target_voxel_affine : _type_
            The affine transformations from voxel to world space for the target
        self_intersection_elements : list[TmsCoilElements]
            The groups of coil elements that need to be checked for self intersection with each other
        affine : npt.NDArray[np.float_]
            The affine transformation from coil to world space

        Returns
        -------
        weighted_target_intersection_quibic_mm : float
            Depth weighted volume intersection between the coil volume and the target volume in mm^3 (volume) * mm (depth)
        self_intersection_quibic_mm : float
            Sum of the volume intersection between the coil element inside the self intersection groups in mm^3
        """
        element_affines = {
            element: element.get_combined_transformation(affine)
            for element in element_voxel_volumes
        }
        element_inv_affines = {
            element: np.linalg.inv(element_affines[element])
            for element in element_voxel_volumes
        }

        target_voxel_inv_affine = np.linalg.inv(target_voxel_affine)
        weighted_target_intersection_quibic_mm = 0
        for element in element_voxel_volumes:
            indexes_in_vox1 = element_voxel_indexes[element]
            element_voxel_affine = element_voxel_affines[element]
            vox_to_vox_affine = (
                target_voxel_inv_affine
                @ element_affines[element]
                @ element_voxel_affine
            )
            indexes_in_vox2 = (
                vox_to_vox_affine[:3, :3] @ indexes_in_vox1.T
                + vox_to_vox_affine[:3, 3, None]
            ).T.astype(np.int32)
            index_mask = np.all(
                np.logical_and(
                    ~np.signbit(indexes_in_vox2),
                    indexes_in_vox2 < target_voxel_distance.shape,
                ),
                axis=1,
            )

            masked_indexes_in_vox2 = indexes_in_vox2[index_mask]
            weighted_target_intersection_quibic_mm += np.sum(
                target_voxel_distance[
                    masked_indexes_in_vox2[:, 0],
                    masked_indexes_in_vox2[:, 1],
                    masked_indexes_in_vox2[:, 2],
                ]
            )

        self_intersection_quibic_mm = 0
        for intersection_group in self_intersection_elements:
            for intersection_pair in itertools.combinations(intersection_group, 2):
                vox_to_vox_affine = (
                    np.linalg.inv(element_voxel_affines[intersection_pair[1]])
                    @ element_inv_affines[intersection_pair[1]]
                    @ element_affines[intersection_pair[0]]
                    @ element_voxel_affines[intersection_pair[0]]
                )
                indexes_in_vox1 = element_voxel_indexes[intersection_pair[0]]
                indexes_in_vox2 = (
                    vox_to_vox_affine[:3, :3] @ indexes_in_vox1.T
                    + vox_to_vox_affine[:3, 3, None]
                ).T.astype(np.int32)

                voxel_volume1 = element_voxel_volumes[intersection_pair[1]]
                index_mask = np.all(
                    np.logical_and(
                        ~np.signbit(indexes_in_vox2),
                        indexes_in_vox2 < voxel_volume1.shape,
                    ),
                    axis=1,
                )
                masked_indexes_in_vox2 = indexes_in_vox2[index_mask]
                self_intersection_quibic_mm += np.count_nonzero(
                    voxel_volume1[
                        masked_indexes_in_vox2[:, 0],
                        masked_indexes_in_vox2[:, 1],
                        masked_indexes_in_vox2[:, 2],
                    ]
                )

        return (
            weighted_target_intersection_quibic_mm,
            self_intersection_quibic_mm,
        )

    def optimize_distance(
        self,
        optimization_surface: Msh,
        affine: npt.NDArray[np.float_],
        coil_translation_ranges: Optional[npt.NDArray[np.float_]] = None,
        coil_rotation_ranges: Optional[npt.NDArray[np.float_]] = None,
        dither_skip=0,
    ) -> tuple[float, float, npt.NDArray[np.float_]]:
        """Optimizes the deformations of the coil elements to minimize the distance between the optimization_surface
        and the min distance points (if not present, the coil casing points) while preventing intersections of the
        optimization_surface and the intersect points (if not present, the coil casing points)

        Parameters
        ----------
        optimization_surface : Msh
            The surface the deformations have to be optimized for
        affine : npt.NDArray[np.float_]
            The affine transformation that is applied to the coil
        coil_translation_ranges : Optional[npt.NDArray[np.float_]], optional
            If the global coil position is supposed to be optimized as well, these ranges in the format
            [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]] are used
            and the updated affine coil transformation is returned, by default None
        coil_translation_ranges : Optional[npt.NDArray[np.float_]], optional
            If the global coil rotation is supposed to be optimized as well, these ranges in the format
            [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]] are used
            and the updated affine coil transformation is returned, by default None
        dither_skip : int, optional
            How many voxel positions should be skipped when creating the coil volume representation.
            Used to speed up the optimization. When set to 0, no dithering will be applied, by default 0

        Returns
        -------
        initial_cost
            The initial cost
        optimized_cost
            The cost after optimization
        result_affine
            The affine matrix. If coil_translation_ranges is None than it's the input affine,
            otherwise it is the optimized affine.

        Raises
        ------
        ValueError
            If the coil has no deformations to optimize
        ValueError
            If the coil has no coil casing or no min distance points
        """

        coil_deformation_ranges = self.get_deformation_ranges()

        if (
            len(coil_deformation_ranges) == 0
            and coil_translation_ranges is None
            and coil_rotation_ranges is None
        ):
            raise ValueError(
                "The coil has no deformations to optimize the coil element positions with."
            )

        if not np.any([np.any(arr) for arr in self.get_casing_coordinates()]):
            raise ValueError("The coil has no coil casing or min_distance points.")

        global_deformations = self.add_global_deformations(
            coil_rotation_ranges, coil_translation_ranges
        )

        (
            element_voxel_volumn,
            element_voxel_indexes,
            element_voxel_affine,
            self_intersection_elements,
        ) = self.get_voxel_volume(global_deformations, dither_skip=dither_skip)
        (
            target_distance_function,
            target_voxel_distance,
            target_voxel_affine,
            cost_surface_tree,
        ) = optimization_surface.get_min_distance_on_grid()
        target_voxel_distance_inside = np.minimum(target_voxel_distance, 0) * -1

        coil_deformation_ranges = self.get_deformation_ranges()
        initial_deformation_settings = np.array(
            [coil_deformation.current for coil_deformation in coil_deformation_ranges]
        )

        def cost_f_x0_w(x):
            for coil_deformation, deformation_setting in zip(
                coil_deformation_ranges, x
            ):
                coil_deformation.current = deformation_setting

            (
                intersection_penalty,
                self_intersection_penalty,
            ) = self._get_fast_intersection_penalty(
                element_voxel_volumn,
                element_voxel_indexes,
                element_voxel_affine,
                target_voxel_distance_inside,
                target_voxel_affine,
                self_intersection_elements,
                affine,
            )
            distance_penalty = self._get_fast_distance_score(
                target_distance_function, element_voxel_volumn.keys(), affine
            )

            f = intersection_penalty + distance_penalty + self_intersection_penalty

            return f

        initial_cost = cost_f_x0_w(initial_deformation_settings)

        direct = opt.direct(
            cost_f_x0_w,
            bounds=[deform.range for deform in coil_deformation_ranges],
            locally_biased=False,
        )
        best_deformation_settings = direct.x

        for coil_deformation, deformation_setting in zip(
            coil_deformation_ranges, best_deformation_settings
        ):
            coil_deformation.current = deformation_setting

        optimized_cost = cost_f_x0_w(best_deformation_settings)

        result_affine = np.eye(4)
        if len(global_deformations) > 0:
            for global_deformation in global_deformations:
                for coil_element in self.elements:
                    coil_element.deformations.remove(global_deformation)
                result_affine = global_deformation.as_matrix() @ result_affine
        result_affine = affine.astype(float) @ result_affine

        return initial_cost, optimized_cost, result_affine

    def get_voxel_volume(
        self, global_deformations: list[TmsCoilDeformation], dither_skip: int = 0
    ) -> tuple[
        dict[TmsCoilElements, npt.NDArray[np.bool_]],
        dict[TmsCoilElements, npt.NDArray[np.int_]],
        dict[TmsCoilElements, npt.NDArray[np.float_]],
        list[TmsCoilElements],
    ]:
        """Generates voxel volume information about the coil.

        Parameters
        ----------
        global_deformations : list[TmsCoilDeformation]
            Global deformations used in the coil
        dither_skip : int, optional
            How many voxel positions should be skipped when creating the coil volume representation.
            Used to speed up the optimization. When set to 0, no dithering will be applied, by default 0

        Returns
        -------
        element_voxel_volume : dict[TmsCoilElements, npt.NDArray[np.bool_]]
            The voxel volume (True inside, False outside) of each coil element
        element_voxel_indexes : dict[TmsCoilElements, npt.NDArray[np.int_]]
            The indexes of the inside voxels for each coil element
        element_voxel_affine : dict[TmsCoilElements, npt.NDArray[np.float_]]
            The affine transformations from world to voxel space for each coil element
        self_intersection_elements : list[TmsCoilElements]
            The groups of coil elements that need to be checked for self intersection with each other
        """
        element_voxel_volume = {}
        element_voxel_indexes = {}
        element_voxel_affine = {}
        if self.casing is not None:
            base_element = DipoleElements(
                None,
                np.zeros((1, 3)),
                np.zeros((1, 3)),
                casing=self.casing,
                deformations=global_deformations,
            )
            (
                element_voxel_volume[base_element],
                element_voxel_affine[base_element],
                element_voxel_indexes[base_element],
                _,
            ) = self.casing.mesh.get_voxel_volume(dither_skip=dither_skip)
        self_intersection_elements = []
        for self_intersection_group in self.self_intersection_test:
            self_intersection_elements.append([])
            for self_intersection_index in self_intersection_group:
                if self_intersection_index == 0:
                    self_intersection_elements[-1].append(base_element)
                else:
                    self_intersection_elements[-1].append(
                        self.elements[self_intersection_index - 1]
                    )
        for element in self.elements:
            if element.casing is not None:
                (
                    element_voxel_volume[element],
                    element_voxel_affine[element],
                    element_voxel_indexes[element],
                    _,
                ) = element.casing.mesh.get_voxel_volume(dither_skip=dither_skip)

        return (
            element_voxel_volume,
            element_voxel_indexes,
            element_voxel_affine,
            self_intersection_elements,
        )

    def add_global_deformations(
        self,
        coil_rotation_ranges: Optional[npt.NDArray[np.float_]] = None,
        coil_translation_ranges: Optional[npt.NDArray[np.float_]] = None,
    ) -> list[TmsCoilDeformation]:
        """Adds deformations to the coil. The deformations are added to all coil elements so that they are global.

        Parameters
        ----------
        coil_rotation_ranges : Optional[npt.NDArray[np.float_]], optional
            Adds global rotations to the coil, the format is [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]], by default None
        coil_translation_ranges : Optional[npt.NDArray[np.float_]], optional
            Adds global deformations to the coil, the format is [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]], by default None

        Returns
        -------
        global_deformations : list[TmsCoilDeformation]
            Returns a list of the added global deformations
        """
        global_deformations = []
        if coil_rotation_ranges is not None:
            if coil_rotation_ranges[0, 0] != coil_rotation_ranges[0, 1]:
                global_deformations.append(
                    TmsCoilRotation(
                        TmsCoilDeformationRange(
                            0, (coil_rotation_ranges[0, 0], coil_rotation_ranges[0, 1])
                        ),
                        [0, 0, 0],
                        [1, 0, 0],
                    )
                )

            if coil_rotation_ranges[1, 0] != coil_rotation_ranges[1, 1]:
                global_deformations.append(
                    TmsCoilRotation(
                        TmsCoilDeformationRange(
                            0, (coil_rotation_ranges[1, 0], coil_rotation_ranges[1, 1])
                        ),
                        [0, 0, 0],
                        [0, 1, 0],
                    )
                )

            if coil_rotation_ranges[2, 0] != coil_rotation_ranges[2, 1]:
                global_deformations.append(
                    TmsCoilRotation(
                        TmsCoilDeformationRange(
                            0, (coil_rotation_ranges[2, 0], coil_rotation_ranges[2, 1])
                        ),
                        [0, 0, 0],
                        [0, 0, 1],
                    )
                )
        if coil_translation_ranges is not None:
            if coil_translation_ranges[0, 0] != coil_translation_ranges[0, 1]:
                global_deformations.append(
                    TmsCoilTranslation(
                        TmsCoilDeformationRange(
                            0,
                            (
                                coil_translation_ranges[0, 0],
                                coil_translation_ranges[0, 1],
                            ),
                        ),
                        0,
                    )
                )
            if coil_translation_ranges[1, 0] != coil_translation_ranges[1, 1]:
                global_deformations.append(
                    TmsCoilTranslation(
                        TmsCoilDeformationRange(
                            0,
                            (
                                coil_translation_ranges[1, 0],
                                coil_translation_ranges[1, 1],
                            ),
                        ),
                        1,
                    )
                )

            if coil_translation_ranges[2, 0] != coil_translation_ranges[2, 1]:
                global_deformations.append(
                    TmsCoilTranslation(
                        TmsCoilDeformationRange(
                            0,
                            (
                                coil_translation_ranges[2, 0],
                                coil_translation_ranges[2, 1],
                            ),
                        ),
                        2,
                    )
                )
        for global_deformation in global_deformations:
            for coil_element in self.elements:
                coil_element.deformations.append(global_deformation)
        return global_deformations

    def optimize_e_mag(
        self,
        head_mesh: Msh,
        region_of_interest_element_mask: npt.NDArray[np.bool_],
        affine: npt.NDArray[np.float_],
        coil_translation_ranges: Optional[npt.NDArray[np.float_]] = None,
        coil_rotation_ranges: Optional[npt.NDArray[np.float_]] = None,
        dither_skip=0,
        fem_evaluation_cutoff=1000,
    ) -> tuple[float, float, npt.NDArray[np.float_], npt.NDArray[np.float_]]:
        """Optimizes the deformations of the coil elements to maximize the mean e-field magnitude in the ROI while preventing intersections of the
        scalp surface and the coil casing

        Parameters
        ----------
        head_mesh : Msh
            The head mesh used in the TMS simulation and the head mesh where the scalp surface is used for coil head intersection
        region_of_interest_element_mask : npt.NDArray[np.bool_]
            Binary mask of which elements will be used to calculate the mean e field magnitude
        affine : npt.NDArray[np.float_]
            The affine transformation that is applied to the coil
        coil_translation_ranges : Optional[npt.NDArray[np.float_]], optional
            If the global coil position is supposed to be optimized as well, these ranges in the format
            [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]] are used
            and the updated affine coil transformation is returned, by default None
        coil_rotation_ranges : Optional[npt.NDArray[np.float_]], optional
            If the global coil rotation is supposed to be optimized as well, these ranges in the format
            [[min(x), max(x)],[min(y), max(y)], [min(z), max(z)]] are used
            and the updated affine coil transformation is returned, by default None
        dither_skip : int, optional
            How many voxel positions should be skipped when creating the coil volume representation.
            Used to speed up the optimization. When set to 0, no dithering will be applied, by default 0
        fem_evaluation_cutoff : int, optional
            If the penalty from the intersection and self intersection is greater than this cutoff value, the fem will not be evaluated to save time.
            Set to np.inf to always evaluate the fem, by default 1000

        Returns
        -------
        initial_cost : float
            The initial cost
        optimized_cost : float
            The cost after the optimization
        result_affine : npt.NDArray[np.float_]
            The optimized affine matrix
        optimized_e_mag : npt.NDArray[np.float_]
            The e field magnitude in the roi elements after the optimization

        Raises
        ------
        ValueError
            If the coil has no deformations to optimize
        ValueError
            If the coil has no casing
        """
        from simnibs.simulation.onlinefem import OnlineFEM

        coil_sampled = self.as_sampled()
        coil_deformation_ranges = coil_sampled.get_deformation_ranges()

        if (
            len(coil_deformation_ranges) == 0
            and coil_translation_ranges is None
            and coil_rotation_ranges is None
        ):
            raise ValueError(
                "The coil has no deformations to optimize the coil element positions with."
            )

        global_deformations = coil_sampled.add_global_deformations(
            coil_rotation_ranges, coil_translation_ranges
        )
        optimization_surface = head_mesh.crop_mesh(tags=[ElementTags.SCALP_TH_SURFACE])

        (
            element_voxel_volume,
            element_voxel_indexes,
            element_voxel_affine,
            self_intersection_elements,
        ) = coil_sampled.get_voxel_volume(global_deformations, dither_skip=dither_skip)
        (
            target_distance_function,
            target_voxel_distance,
            target_voxel_affine,
            cost_surface_tree,
        ) = optimization_surface.get_min_distance_on_grid()
        target_voxel_distance_inside = np.minimum(target_voxel_distance, 0) * -1

        dither_factor = 1
        if dither_skip > 1:
            volume_in_mm3 = 0
            dither_volume_in_mm3 = 0
            for element in element_voxel_volume:
                volume_in_mm3 += np.count_nonzero(element_voxel_volume[element])
                dither_volume_in_mm3 += len(element_voxel_indexes[element])

            dither_factor = volume_in_mm3 / dither_volume_in_mm3

        coil_deformation_ranges = coil_sampled.get_deformation_ranges()

        if len(element_voxel_volume) == 0:
            raise ValueError(
                "The coil has no coil casing to be used for coil head intersection tests."
            )

        roi = RegionOfInterest(
            head_mesh,
            center=head_mesh.elements_baricenters()[region_of_interest_element_mask],
        )
        fem = OnlineFEM(
            head_mesh, "TMS", roi, coil=coil_sampled, dataType=[0], useElements=False
        )

        initial_deformation_settings = np.array(
            [coil_deformation.current for coil_deformation in coil_deformation_ranges]
        )

        def cost_f_x0_w(x):
            for coil_deformation, deformation_setting in zip(
                coil_deformation_ranges, x
            ):
                coil_deformation.current = deformation_setting
            (
                intersection_penalty,
                self_intersection_penalty,
            ) = coil_sampled._get_fast_intersection_penalty(
                element_voxel_volume,
                element_voxel_indexes,
                element_voxel_affine,
                target_voxel_distance_inside,
                target_voxel_affine,
                self_intersection_elements,
                affine,
            )

            penalty = (intersection_penalty + self_intersection_penalty) * dither_factor
            if penalty > fem_evaluation_cutoff:
                return penalty

            roi_e_field = fem.update_field(matsimnibs=affine)

            f = penalty - 100 * np.mean(roi_e_field)

            return f

        initial_cost = cost_f_x0_w(initial_deformation_settings)

        direct = opt.direct(
            cost_f_x0_w,
            bounds=[deform.range for deform in coil_deformation_ranges],
            locally_biased=False,
        )
        best_deformation_settings = direct.x

        for sampled_coil_deformation, deformation_setting in zip(
            coil_deformation_ranges, best_deformation_settings
        ):
            sampled_coil_deformation.current = deformation_setting

        optimized_cost = cost_f_x0_w(best_deformation_settings)
        optimized_e_mag = np.ravel(fem.update_field(matsimnibs=affine))

        result_affine = np.eye(4)
        if len(global_deformations) > 0:
            for global_deformation in global_deformations:
                for coil_element in coil_sampled.elements:
                    coil_element.deformations.remove(global_deformation)
                result_affine = global_deformation.as_matrix() @ result_affine
        result_affine = affine.astype(float) @ result_affine

        for sampled_coil_deformation, coil_deformation in zip(
            coil_sampled.get_deformation_ranges(), self.get_deformation_ranges()
        ):
            coil_deformation.current = sampled_coil_deformation.current

        return initial_cost, optimized_cost, result_affine, optimized_e_mag
