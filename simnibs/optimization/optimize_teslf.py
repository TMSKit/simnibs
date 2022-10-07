import copy
import csv
import re
import os
import time
import glob
import functools
import logging
import gc

import numpy as np
import scipy.spatial
import h5py
import nibabel

from . import optimize_tms
from . import optimization_methods
from . import ADMlib
from ..simulation import fem
from ..simulation import cond
from ..simulation.sim_struct import SESSION, TMSLIST, SimuList, save_matlab_sim_struct
from ..mesh_tools import mesh_io, gmsh_view
from ..utils import transformations
from ..utils.simnibs_logger import logger
from ..utils.file_finder import SubjectFiles
from ..utils.matlab_read import try_to_read_matlab_field, remove_None

# TODO: adapt imports

class TDCSoptimize():
    ''' Defines a tdcs optimization problem

    Parameters
    --------------
    leadfield_hdf: str (optional)
        Name of file with leadfield
    max_total_current: float (optional)
        Maximum current across all electrodes (in Amperes). Default: 2e-3
    max_individual_current: float (optional)
        Maximum current for any single electrode (in Amperes). Default: 1e-3
    max_active_electrodes: int (optional)
        Maximum number of active electrodes. Default: no maximum
    name: str (optional)
        Name of optimization problem. Default: optimization
    target: list of TDCStarget objects (optional)
        Targets for the optimization. Default: no target
    avoid: list of TDCSavoid objects
        list of TDCSavoid objects defining regions to avoid


    Attributes
    --------------
    leadfield_hdf: str
        Name of file with leadfield
    max_total_current: float (optional)
        Maximum current across all electrodes (in Amperes). Default: 2e-3
    max_individual_current: float
        Maximum current for any single electrode (in Amperes). Default: 1e-3
    max_active_electrodes: int
        Maximum number of active electrodes. Default: no maximum

    ledfield_path: str
        Path to the leadfield in the hdf5 file. Default: '/mesh_leadfield/leadfields/tdcs_leadfield'
    mesh_path: str
        Path to the mesh in the hdf5 file. Default: '/mesh_leadfield/'

    The two above are used to define:

    mesh: simnibs.msh.mesh_io.Msh
        Mesh with problem geometry

    leadfield: np.ndarray
        Leadfield matrix (N_elec -1 x M x 3) where M is either the number of nodes or the
        number of elements in the mesh. We assume that there is a reference electrode

    Alternatively, you can set the three attributes above and not leadfield_path,
    mesh_path and leadfield_hdf

    lf_type: None, 'node' or 'element'
        Type of leadfield.

    name: str
        Name for the optimization problem. Defaults tp 'optimization'

    target: list of TDCStarget objects
        list of TDCStarget objects defining the targets of the optimization

    avoid: list of TDCSavoid objects (optional)
        list of TDCSavoid objects defining regions to avoid

    open_in_gmsh: bool (optional)
        Whether to open the result in Gmsh after the calculations. Default: False

    Warning
    -----------
    Changing leadfield_hdf, leadfield_path and mesh_path after constructing the class
    can cause unexpected behaviour
    '''

    def __init__(self, leadfield_hdf=None,
                 max_total_current=2e-3,
                 max_individual_current=1e-3,
                 max_active_electrodes=None,
                 name='optimization/tdcs',
                 target=None,
                 avoid=None,
                 open_in_gmsh=True):
        self.leadfield_hdf = leadfield_hdf
        self.max_total_current = max_total_current
        self.max_individual_current = max_individual_current
        self.max_active_electrodes = max_active_electrodes
        self.leadfield_path = '/mesh_leadfield/leadfields/tdcs_leadfield'
        self.mesh_path = '/mesh_leadfield/'
        self.open_in_gmsh = open_in_gmsh
        self._mesh = None
        self._leadfield = None
        self._field_name = None
        self._field_units = None
        self.name = name
        # I can't put [] in the arguments for weird reasons (it gets the previous value)
        if target is None:
            self.target = []
        else:
            self.target = target
        if avoid is None:
            self.avoid = []
        else:
            self.avoid = avoid

    @property
    def lf_type(self):
        if self.mesh is None or self.leadfield is None:
            return None
        if self.leadfield.shape[1] == self.mesh.nodes.nr:
            return 'node'
        elif self.leadfield.shape[1] == self.mesh.elm.nr:
            return 'element'
        else:
            raise ValueError('Could not find if the leadfield is node- or '
                             'element-based')

    @property
    def leadfield(self):
        ''' Reads the leadfield from the HDF5 file'''
        if self._leadfield is None and self.leadfield_hdf is not None:
            with h5py.File(self.leadfield_hdf, 'r') as f:
                self.leadfield = f[self.leadfield_path][:]

        return self._leadfield

    @leadfield.setter
    def leadfield(self, leadfield):
        if leadfield is not None:
            assert leadfield.ndim == 3, 'leadfield should be 3 dimensional'
            assert leadfield.shape[2] == 3, 'Size of last dimension of leadfield should be 3'
        self._leadfield = leadfield

    @property
    def mesh(self):
        if self._mesh is None and self.leadfield_hdf is not None:
            self.mesh = mesh_io.Msh.read_hdf5(self.leadfield_hdf, self.mesh_path)

        return self._mesh

    @mesh.setter
    def mesh(self, mesh):
        elm_type = np.unique(mesh.elm.elm_type)
        if len(elm_type) > 1:
            raise ValueError('Mesh has both tetrahedra and triangles')
        else:
            self._mesh = mesh

    @property
    def field_name(self):
        if self.leadfield_hdf is not None and self._field_name is None:
            try:
                with h5py.File(self.leadfield_hdf, 'r') as f:
                    self.field_name = f[self.leadfield_path].attrs['field']
            except:
                return 'Field'

        if self._field_name is None:
            return 'Field'
        else:
            return self._field_name

    @field_name.setter
    def field_name(self, field_name):
        self._field_name = field_name

    @property
    def field_units(self):
        if self.leadfield_hdf is not None and self._field_units is None:
            try:
                with h5py.File(self.leadfield_hdf, 'r') as f:
                    self.field_units = f[self.leadfield_path].attrs['units']
            except:
                return 'Au'

        if self._field_units is None:
            return 'Au'
        else:
            return self._field_units

    @field_units.setter
    def field_units(self, field_units):
        self._field_units = field_units

    def to_mat(self):
        """ Makes a dictionary for saving a matlab structure with scipy.io.savemat()

        Returns
        --------------------
        dict
            Dictionaty for usage with scipy.io.savemat
        """
        mat = {}
        mat['type'] = 'TDCSoptimize'
        mat['leadfield_hdf'] = remove_None(self.leadfield_hdf)
        mat['max_total_current'] = remove_None(self.max_total_current)
        mat['max_individual_current'] = remove_None(self.max_individual_current)
        mat['max_active_electrodes'] = remove_None(self.max_active_electrodes)
        mat['open_in_gmsh'] = remove_None(self.open_in_gmsh)
        mat['name'] = remove_None(self.name)
        mat['target'] = _save_TDCStarget_mat(self.target)
        mat['avoid'] = _save_TDCStarget_mat(self.avoid)
        return mat

    @classmethod
    def read_mat_struct(cls, mat):
        '''Reads a .mat structure

        Parameters
        -----------
        mat: dict
            Dictionary from scipy.io.loadmat

        Returns
        ----------
        p: TDCSoptimize
            TDCSoptimize structure
        '''
        t = cls()
        leadfield_hdf = try_to_read_matlab_field(
            mat, 'leadfield_hdf', str, t.leadfield_hdf)
        max_total_current = try_to_read_matlab_field(
            mat, 'max_total_current', float, t.max_total_current)
        max_individual_current = try_to_read_matlab_field(
            mat, 'max_individual_current', float, t.max_individual_current)
        max_active_electrodes = try_to_read_matlab_field(
            mat, 'max_active_electrodes', int, t.max_active_electrodes)
        open_in_gmsh = try_to_read_matlab_field(
            mat, 'open_in_gmsh', bool, t.open_in_gmsh)
        name = try_to_read_matlab_field(
            mat, 'name', str, t.name)
        target = []
        if len(mat['target']) > 0:
            for t in mat['target'][0]:
                target_struct = TDCStarget.read_mat_struct(t)
                if target_struct is not None:
                    target.append(target_struct)
        if len(target) == 0:
            target = None

        avoid = []
        if len(mat['avoid']) > 0:
            avoid = []
            for t in mat['avoid'][0]:
                avoid_struct = TDCSavoid.read_mat_struct(t)
                if avoid_struct is not None:
                    avoid.append(avoid_struct)
        if len(avoid) == 0:
            avoid = None

        return cls(leadfield_hdf, max_total_current,
                   max_individual_current, max_active_electrodes,
                   name, target, avoid, open_in_gmsh)

    def get_weights(self):
        ''' Calculates the volumes or areas of the mesh associated with the leadfield
        '''
        assert self.mesh is not None, 'Mesh not defined'
        if self.lf_type == 'node':
            weights = self.mesh.nodes_volumes_or_areas().value
        elif self.lf_type == 'element':
            weights = self.mesh.elements_volumes_and_areas().value
        else:
            raise ValueError('Cant calculate weights: mesh or leadfield not set')

        weights *= self._get_avoid_field()
        return weights

    def _get_avoid_field(self):
        fields = []
        for a in self.avoid:
            a.mesh = self.mesh
            a.lf_type = self.lf_type
            fields.append(a.avoid_field())

        if len(fields) > 0:
            total_field = np.ones_like(fields[0])
            for f in fields:
                total_field *= f
            return total_field

        else:
            return 1.

    def add_target(self, target=None):
        ''' Adds a target to the current tDCS optimization

        Parameters:
        ------------
        target: TDCStarget (optional)
            TDCStarget structure to be added. Default: empty TDCStarget

        Returns:
        -----------
        target: TDCStarget
            TDCStarget added to the structure
        '''
        if target is None:
            target = TDCStarget(mesh=self.mesh, lf_type=self.lf_type)
        self.target.append(target)
        return target

    def add_avoid(self, avoid=None):
        ''' Adds an avoid structure to the current tDCS optimization

        Parameters:
        ------------
        target: TDCStarget (optional)
            TDCStarget structure to be added. Default: empty TDCStarget

        Returns:
        -----------
        target: TDCStarget
            TDCStarget added to the structure
        '''
        if avoid is None:
            avoid = TDCSavoid(mesh=self.mesh, lf_type=self.lf_type)
        self.avoid.append(avoid)
        return avoid

    def _assign_mesh_lf_type_to_target(self):
        for t in self.target:
            if t.mesh is None: t.mesh = self.mesh
            if t.lf_type is None: t.lf_type = self.lf_type
        for a in self.avoid:
            if a.mesh is None: a.mesh = self.mesh
            if a.lf_type is None: a.lf_type = self.lf_type

    def optimize(self, fn_out_mesh=None, fn_out_csv=None):
        ''' Runs the optimization problem

        Parameters
        -------------
        fn_out_mesh: str
            If set, will write out the electric field and currents to the mesh

        fn_out_mesh: str
            If set, will write out the currents and electrode names to a CSV file


        Returns
        ------------
        currents: N_elec x 1 ndarray
            Optimized currents. The first value is the current in the reference electrode
        '''
        assert len(self.target) > 0, 'No target defined'
        assert self.leadfield is not None, 'Leadfield not defined'
        assert self.mesh is not None, 'Mesh not defined'
        if self.max_active_electrodes is not None:
            assert self.max_active_electrodes > 1, \
                'The maximum number of active electrodes should be at least 2'

        if self.max_total_current is None:
            logger.warning('Maximum total current not set!')
            max_total_current = 1e3

        else:
            assert self.max_total_current > 0
            max_total_current = self.max_total_current

        if self.max_individual_current is None:
            max_individual_current = max_total_current

        else:
            assert self.max_individual_current > 0
            max_individual_current = self.max_individual_current

        self._assign_mesh_lf_type_to_target()
        weights = self.get_weights()
        norm_constrained = [t.directions is None for t in self.target]

        # Angle-constrained optimization
        if any([t.max_angle is not None for t in self.target]):
            if len(self.target) > 1:
                raise ValueError("Can't apply angle constraints with multiple target")
            t = self.target[0]
            max_angle = t.max_angle
            indices, directions = t.get_indexes_and_directions()
            assert max_angle > 0, 'max_angle must be >= 0'
            if self.max_active_electrodes is None:
                opt_problem = optimization_methods.TESLinearAngleConstrained(
                    indices, directions,
                    t.target_mean, max_angle, self.leadfield,
                    max_total_current, max_individual_current,
                    weights=weights, weights_target=t.get_weights()
                )

            else:
                opt_problem = optimization_methods.TESLinearAngleElecConstrained(
                    self.max_active_electrodes, indices, directions,
                    t.target_mean, max_angle, self.leadfield,
                    max_total_current, max_individual_current,
                    weights, weights_target=t.get_weights()
                )

        # Norm-constrained optimization
        elif any(norm_constrained):
            if not all(norm_constrained):
                raise ValueError("Can't mix norm and linear constrained optimization")
            if self.max_active_electrodes is None:
                opt_problem = optimization_methods.TESNormConstrained(
                    self.leadfield, max_total_current,
                    max_individual_current, weights
                )
            else:
                opt_problem = optimization_methods.TESNormElecConstrained(
                    self.max_active_electrodes,
                    self.leadfield, max_total_current,
                    max_individual_current, weights
                )
            for t in self.target:
                if t.intensity < 0:
                    raise ValueError('Intensity must be > 0')
                opt_problem.add_norm_constraint(
                    t.get_indexes_and_directions()[0], t.intensity,
                    t.get_weights()
                )

        # Simple QP-style optimization
        else:
            if self.max_active_electrodes is None:
                opt_problem = optimization_methods.TESLinearConstrained(
                    self.leadfield, max_total_current,
                    max_individual_current, weights)

            else:
                opt_problem = optimization_methods.TESLinearElecConstrained(
                    self.max_active_electrodes, self.leadfield,
                    max_total_current, max_individual_current, weights)

            for t in self.target:
                opt_problem.add_linear_constraint(
                    *t.get_indexes_and_directions(), t.intensity,
                    t.get_weights()
                )

        currents = opt_problem.solve()

        logger.log(25, '\n' + self.summary(currents))

        if fn_out_mesh is not None:
            fn_out_mesh = os.path.abspath(fn_out_mesh)
            m = self.field_mesh(currents)
            m.write(fn_out_mesh)
            v = m.view()
            ## Configure view
            v.Mesh.SurfaceFaces = 0
            v.View[0].Visible = 1
            # Change vector type for target field
            offset = 2
            if self.lf_type == 'node':
                offset = 3
            for i, t in enumerate(self.target):
                v.View[offset + i].VectorType = 4
                v.View[offset + i].ArrowSizeMax = 60
                v.View[offset + i].Visible = 1
            # Electrode geo file
            el_geo_fn = os.path.splitext(fn_out_mesh)[0] + '_el_currents.geo'
            self.electrode_geo(el_geo_fn, currents)
            v.add_merge(el_geo_fn)
            max_c = np.max(np.abs(currents))
            v.add_view(Visible=1, RangeType=2,
                       ColorTable=gmsh_view._coolwarm_cm(),
                       CustomMax=max_c, CustomMin=-max_c,
                       PointSize=10)
            v.write_opt(fn_out_mesh)
            if self.open_in_gmsh:
                mesh_io.open_in_gmsh(fn_out_mesh, True)

        if fn_out_csv is not None:
            self.write_currents_csv(currents, fn_out_csv)

        return currents

    def field(self, currents):
        ''' Outputs the electric fields caused by the current combination

        Parameters
        -----------
        currents: N_elec x 1 ndarray
            Currents going through each electrode, in A. Usually from the optimize
            method. The sum should be approximately zero

        Returns
        ----------
        E: simnibs.mesh.NodeData or simnibs.mesh.ElementData
            NodeData or ElementData with the field caused by the currents
        '''

        assert np.isclose(np.sum(currents), 0, atol=1e-5), 'Currents should sum to zero'
        E = np.einsum('ijk,i->jk', self.leadfield, currents[1:])

        if self.lf_type == 'node':
            E = mesh_io.NodeData(E, self.field_name, mesh=self.mesh)

        if self.lf_type == 'element':
            E = mesh_io.ElementData(E, self.field_name, mesh=self.mesh)

        return E

    def electrode_geo(self, fn_out, currents=None, mesh_elec=None, elec_tags=None,
                      elec_positions=None):
        ''' Creates a mesh with the electrodes and their currents

        Parameters
        ------------
        currents: N_elec x 1 ndarray (optional)
            Electric current values per electrode. Default: do not print currents
        mesh_elec: simnibs.mesh.Msh (optional)
            Mesh with the electrodes. Default: look for a mesh called mesh_electrodes in
            self.leadfield_hdf
        elec_tags: N_elec x 1 ndarray of ints (optional)
            Tags of the electrodes corresponding to each leadfield column. The first is
            the reference electrode. Default: load at the attribute electrode_tags in the
            leadfield dataset
        elec_positions: N_elec x 3 ndarray of floats (optional)
            Positions of the electrodes in the head. If mesh_elec is not defined, will
            create small sphres at those positions instead.
            Default: load at the attribute electrode_pos in the leadfield dataset

        '''
        # First try to set the electrode visualizations using meshed electrodes
        if mesh_elec is None:
            if self.leadfield_hdf is not None:
                try:
                    mesh_elec = mesh_io.Msh.read_hdf5(self.leadfield_hdf, 'mesh_electrodes')
                except KeyError:
                    pass
            else:
                raise ValueError('Please define a mesh with the electrodes')

        if elec_tags is None and mesh_elec is not None:
            if self.leadfield_hdf is not None:
                with h5py.File(self.leadfield_hdf, 'r') as f:
                    elec_tags = f[self.leadfield_path].attrs['electrode_tags']
            else:
                raise ValueError('Please define the electrode tags')

        # If not, use point electrodes
        if mesh_elec is None and elec_positions is None:
            if self.leadfield_hdf is not None:
                with h5py.File(self.leadfield_hdf, 'r') as f:
                    elec_positions = f[self.leadfield_path].attrs['electrode_pos']
            else:
                raise ValueError('Please define the electrode positions')

        if mesh_elec is not None:
            elec_pos = self._electrode_geo_triangles(fn_out, currents, mesh_elec, elec_tags)
            # elec_pos is used for writing electrode names
        elif elec_positions is not None:
            self._electrode_geo_points(fn_out, currents, elec_positions)
            elec_pos = elec_positions
        else:
            raise ValueError('Neither mesh_elec nor elec_positions defined')
        if self.leadfield_hdf is not None:
            with h5py.File(self.leadfield_hdf, 'r') as f:
                try:
                    elec_names = f[self.leadfield_path].attrs['electrode_names']
                    elec_names = [n.decode() if isinstance(n, bytes) else n for n in elec_names]
                except KeyError:
                    elec_names = None

            if elec_names is not None:
                mesh_io.write_geo_text(
                    elec_pos, elec_names,
                    fn_out, name="electrode_names", mode='ba')

    def _electrode_geo_triangles(self, fn_out, currents, mesh_elec, elec_tags):
        if currents is None:
            currents = np.ones(len(elec_tags))

        assert len(elec_tags) == len(currents), 'Define one current per electrode'

        triangles = []
        values = []
        elec_pos = []
        bar = mesh_elec.elements_baricenters()
        norms = mesh_elec.triangle_normals()
        for t, c in zip(elec_tags, currents):
            triangles.append(mesh_elec.elm[mesh_elec.elm.tag1 == t, :3])
            values.append(c * np.ones(len(triangles[-1])))
            avg_norm = np.average(norms[mesh_elec.elm.tag1 == t], axis=0)
            pos = np.average(bar[mesh_elec.elm.tag1 == t], axis=0)
            pos += avg_norm * 4
            elec_pos.append(pos)

        triangles = np.concatenate(triangles, axis=0)
        values = np.concatenate(values, axis=0)
        elec_pos = np.vstack(elec_pos)
        mesh_io.write_geo_triangles(
            triangles - 1, mesh_elec.nodes.node_coord,
            fn_out, values, 'electrode_currents')

        return elec_pos

    def _electrode_geo_points(self, fn_out, currents, elec_positions):
        if currents is None:
            currents = np.ones(len(elec_positions))

        assert len(elec_positions) == len(currents), 'Define one current per electrode'
        mesh_io.write_geo_spheres(elec_positions, fn_out, currents, "electrode_currents")

    def field_mesh(self, currents):
        ''' Creates showing the targets and the field
        Parameters
        -------------
        currents: N_elec x 1 ndarray
            Currents going through each electrode, in A. Usually from the optimize
            method. The sum should be approximately zero

        Returns
        ---------
        results: simnibs.msh.mesh_io.Msh
            Mesh file
        '''
        target_fields = [t.as_field('target_{0}'.format(i + 1)) for i, t in
                         enumerate(self.target)]
        weight_fields = [t.as_field('avoid_{0}'.format(i + 1)) for i, t in
                         enumerate(self.avoid)]
        e_field = self.field(currents)
        e_magn_field = e_field.norm()
        if self.lf_type == 'node':
            normals = -self.mesh.nodes_normals()[:]
            e_normal_field = np.sum(e_field[:] * normals, axis=1)
            e_normal_field = mesh_io.NodeData(e_normal_field, 'normal' + e_field.field_name, mesh=self.mesh)
        m = copy.deepcopy(self.mesh)
        if self.lf_type == 'node':
            m.nodedata = [e_magn_field, e_field, e_normal_field] + target_fields + weight_fields
        elif self.lf_type == 'element':
            m.elmdata = [e_magn_field, e_field] + target_fields + weight_fields
        return m

    def write_currents_csv(self, currents, fn_csv, electrode_names=None):
        ''' Writes the currents and the corresponding electrode names to a CSV file

        Parameters
        ------------
        currents: N_elec x 1 ndarray
            Array with electrode currents
        fn_csv: str
            Name of CSV file to write
        electrode_names: list of strings (optional)
            Name of electrodes. Default: will read from the electrode_names attribute in
            the leadfield dataset
        '''
        if electrode_names is None:
            if self.leadfield_hdf is not None:
                with h5py.File(self.leadfield_hdf, 'r') as f:
                    electrode_names = f[self.leadfield_path].attrs['electrode_names']
                    electrode_names = [n.decode() if isinstance(n, bytes) else n for n in electrode_names]
            else:
                raise ValueError('Please define the electrode names')

        assert len(electrode_names) == len(currents)
        with open(fn_csv, 'w', newline='') as f:
            writer = csv.writer(f)
            for n, c in zip(electrode_names, currents):
                writer.writerow([n, c])

    def run(self, cpus=1):
        ''' Interface to use with the run_simnibs function

        Parameters
        ---------------
        cpus: int (optional)
            Does not do anything, it is just here for the common interface with the
            simulation's run function
        '''
        if not self.name:
            if self.leadfield_hdf is not None:
                try:
                    name = re.search(r'(.+)_leadfield_', self.leadfield_hdf).group(1)
                except AttributeError:
                    name = 'optimization'
            else:
                name = 'optimization'
        else:
            name = self.name
        out_folder = os.path.dirname(name)
        os.makedirs(out_folder, exist_ok=True)

        # Set-up logger
        fh = logging.FileHandler(name + '.log', mode='w')
        formatter = logging.Formatter(
            '[ %(name)s - %(asctime)s - %(process)d ]%(levelname)s: %(message)s')
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

        fn_summary = name + '_summary.txt'
        fh_s = logging.FileHandler(fn_summary, mode='w')
        fh_s.setFormatter(logging.Formatter('%(message)s'))
        fh_s.setLevel(25)
        logger.addHandler(fh_s)

        fn_out_mesh = name + '.msh'
        fn_out_csv = name + '.csv'
        logger.info('Optimizing')
        logger.log(25, str(self))
        self.optimize(fn_out_mesh, fn_out_csv)
        logger.log(
            25,
            '\n=====================================\n'
            'SimNIBS finished running optimization\n'
            'Mesh file: {0}\n'
            'CSV file: {1}\n'
            'Summary file: {2}\n'
            '====================================='
            .format(fn_out_mesh, fn_out_csv, fn_summary))

        logger.removeHandler(fh)
        logger.removeHandler(fh_s)

        return fn_out_mesh

    def __str__(self):
        s = 'Optimization set-up\n'
        s += '===========================\n'
        s += 'Leadfield file: {0}\n'.format(self.leadfield_hdf)
        s += 'Max. total current: {0} (A)\n'.format(self.max_total_current)
        s += 'Max. individual current: {0} (A)\n'.format(self.max_individual_current)
        s += 'Max. active electrodes: {0}\n'.format(self.max_active_electrodes)
        s += 'Name: {0}\n'.format(self.name)
        s += '----------------------\n'
        s += 'N targets: {0}\n'.format(len(self.target))
        s += '......................\n'.join(
            ['Target {0}:\n{1}'.format(i + 1, str(t)) for i, t in
             enumerate(self.target)])
        s += '----------------------\n'
        s += 'N avoid: {0}\n'.format(len(self.avoid))
        s += '......................\n'.join(
            ['Avoid {0}:\n{1}'.format(i + 1, str(t)) for i, t in
             enumerate(self.avoid)])
        return s

    def summary(self, currents):
        ''' Returns a string with a summary of the optimization

        Parameters
        ------------
        field: ElementData or NodeData
            Field of interest

        Returns
        ------------
        summary: str
            Summary of field
        '''
        s = 'Optimization Summary\n'
        s += '=============================\n'
        s += 'Total current: {0:.2e} (A)\n'.format(np.linalg.norm(currents, ord=1) / 2)
        s += 'Maximum current: {0:.2e} (A)\n'.format(np.max(np.abs(currents)))
        s += 'Active electrodes: {0}\n'.format(int(np.linalg.norm(currents, ord=0)))
        field = self.field(currents)
        s += 'Field Summary\n'
        s += '----------------------------\n'
        s += 'Peak Value (99.9 percentile): {0:.2f} ({1})\n'.format(
            field.get_percentiles(99.9)[0], self.field_units)
        s += 'Mean field magnitude: {0:.2e} ({1})\n'.format(
            field.mean_field_norm(), self.field_units)
        if np.any(self.mesh.elm.elm_type == 4):
            v_units = 'mm3'
        else:
            v_units = 'mm2'
        s += 'Focality: 50%: {0:.2e} 70%: {1:.2e} ({2})\n'.format(
            *field.get_focality(cuttofs=[50, 70], peak_percentile=99.9),
            v_units)
        for i, t in enumerate(self.target):
            s += 'Target {0}\n'.format(i + 1)
            s += '    Intensity specified:{0:.2f} achieved: {1:.2f} ({2})\n'.format(
                t.intensity, t.mean_intensity(field), self.field_units)
            if t.max_angle is not None:
                s += ('    Average angle across target: {0:.1f} '
                      '(max set to {1:.1f}) (degrees)\n'.format(
                    t.mean_angle(field), t.max_angle))
            else:
                s += '    Average angle across target: {0:.1f} (degrees)\n'.format(
                    t.mean_angle(field))

        for i, a in enumerate(self.avoid):
            s += 'Avoid {0}\n'.format(i + 1)
            s += '    Mean field magnitude in region: {0:.2e} ({1})\n'.format(
                a.mean_field_norm_in_region(field), self.field_units)

        return s


class TDCStarget:
    ''' Defines a target for TDCS optimization

    Attributes
    -------------
    positions: Nx3 ndarray
        List of target positions, in x, y, z coordinates and in the subject space. Will find the closes mesh points
    indexes: Nx1 ndarray of ints
        Indexes (1-based) of elements/nodes for optimization. Overwrites positions
    directions: Nx3 ndarray
        List of Electric field directions to be optimized for each mesh point, the string
        'normal' or None (for magnitude optimization), Default: 'normal'
    intensity: float (optional)
        Target intensity of the electric field component in V/m. Default: 0.2
    max_angle: float (optional)
        Maximum angle between electric field and target direction, in degrees. Default:
        No maximum
    radius: float (optional)
        Radius of target. All the elements/nodes within the given radies of the indexes
        will be included.
    tissues: list or None (Optional)
        Tissues included in the target. Either a list of integer with tissue tags or None
        for all tissues. Default: None

    THE ONES BELOW SHOULD NOT BE FILLED BY THE USERS IN NORMAL CIRCUNTANCES:
    mesh: simnibs.msh.mesh_io.Msh (optional)
        Mesh where the target is defined. Set by the TDCSoptimize methods
    lf_type: 'node' or 'element'
        Where the electric field values are defined

    '''

    def __init__(self, positions=None, indexes=None, directions='normal',
                 intensity=0.2, max_angle=None, radius=2, tissues=None,
                 mesh=None, lf_type=None):

        self.lf_type = lf_type
        self.mesh = mesh
        self.radius = radius
        self.tissues = tissues
        self.positions = positions
        self.indexes = indexes
        self.intensity = intensity
        self.max_angle = max_angle
        self.directions = directions

    @property
    def directions(self):
        return self._directions

    @directions.setter
    def directions(self, value):
        if value == 'normal':
            pass
        elif value == 'none':
            value = None
        elif isinstance(value, str):
            raise ValueError(
                'Invalid value for directions: f{directions} '
                'valid arguments are "normal", "none" or an array'
            )
        if value is None and self.max_angle is not None:
            raise ValueError(
                "Can't constrain angle in magnitude optimizations"
            )
        self._directions = value

    @classmethod
    def read_mat_struct(cls, mat):
        '''Reads a .mat structure

        Parameters
        -----------
        mat: dict
            Dictionary from scipy.io.loadmat

        Returns
        ----------
        t: TDCStarget
            TDCStarget structure
        '''
        t = cls()
        positions = try_to_read_matlab_field(mat, 'positions', list, t.positions)
        indexes = try_to_read_matlab_field(mat, 'indexes', list, t.indexes)
        directions = try_to_read_matlab_field(mat, 'directions', list, t.directions)
        try:
            directions[0]
        except IndexError:
            directions = 'normal'
        else:
            if isinstance(directions[0], str):
                directions = ''.join(directions)
            if isinstance(directions[0], bytes):
                directions = ''.join([d.decode() for d in directions])
        intensity = try_to_read_matlab_field(mat, 'intensity', float, t.intensity)
        max_angle = try_to_read_matlab_field(mat, 'max_angle', float, t.max_angle)
        radius = try_to_read_matlab_field(mat, 'radius', float, t.radius)
        tissues = try_to_read_matlab_field(mat, 'tissues', list, t.tissues)
        if positions is not None and len(positions) == 0:
            positions = None
        if indexes is not None and len(indexes) == 0:
            indexes = None
        if tissues is not None and len(tissues) == 0:
            tissues = None

        is_empty = True
        is_empty *= t.positions == positions
        is_empty *= t.indexes == indexes
        is_empty *= t.directions == directions
        is_empty *= t.intensity == intensity
        is_empty *= t.max_angle == max_angle
        is_empty *= t.tissues == tissues
        if is_empty:
            return None

        return cls(positions, indexes, directions, intensity, max_angle, radius, tissues)

    def get_weights(self):
        assert self.lf_type is not None, 'Please set a lf_type'

        if self.lf_type == 'node':
            weights = self.mesh.nodes_volumes_or_areas().value
        elif self.lf_type == 'element':
            weights = self.mesh.elements_volumes_and_areas().value
        else:
            raise ValueError('Invalid lf_type: {0}, should be '
                             '"element" or "node"'.format(self.lf_type))

        return weights

    def get_indexes_and_directions(self):
        ''' Calculates the mesh indexes and directions corresponding to this target
        Returns
        ----------
        indexes: (n,) ndarray of ints
            0-based region indexes

        indexes: (n,3) ndarray of floats
            Target directions
        '''
        indexes, mapping = _find_indexes(self.mesh, self.lf_type,
                                         positions=self.positions,
                                         indexes=self.indexes,
                                         tissues=self.tissues,
                                         radius=self.radius)

        directions = _find_directions(self.mesh, self.lf_type,
                                      self.directions, indexes,
                                      mapping)

        return indexes - 1, directions

    def as_field(self, name='target_field'):
        ''' Returns the target as an ElementData or NodeData field

        Parameters
        -----------
        name: str
            Name of the field. Default: 'target_field'
        Returns
        ---------
        target: ElementData or NodeData
            A vector field with a vector pointing in the given direction in the target
        '''
        if (self.positions is None) == (self.indexes is None):  # negative XOR operation
            raise ValueError('Please set either positions or indexes')

        assert self.mesh is not None, 'Please set a mesh'

        if self.directions is None:
            nr_comp = 1
        else:
            nr_comp = 3

        if self.lf_type == 'node':
            field = np.zeros((self.mesh.nodes.nr, nr_comp))
            field_type = mesh_io.NodeData
        elif self.lf_type == 'element':
            field = np.zeros((self.mesh.elm.nr, nr_comp))
            field_type = mesh_io.ElementData
        else:
            raise ValueError("lf_type must be 'node' or 'element'."
                             " Got: {0} instead".format(self.lf_type))

        indexes, mapping = _find_indexes(self.mesh, self.lf_type,
                                         positions=self.positions,
                                         indexes=self.indexes,
                                         tissues=self.tissues,
                                         radius=self.radius)

        if self.directions is None:
            field[indexes - 1] = self.intensity
        else:
            directions = _find_directions(
                self.mesh, self.lf_type,
                self.directions, indexes,
                mapping
            )
            field[indexes - 1] = directions * self.intensity

        return field_type(field, name, mesh=self.mesh)

    def mean_intensity(self, field):
        ''' Calculates the mean intensity of the given field in this target

        Parameters
        -----------
        field: Nx3 NodeData or ElementData
            Electric field

        Returns
        ------------
        intensity: float
            Mean intensity in this target and in the target direction
        '''
        if (self.positions is None) == (self.indexes is None):  # negative XOR operation
            raise ValueError('Please set either positions or indexes')

        assert self.mesh is not None, 'Please set a mesh'
        assert field.nr_comp == 3, 'Field must have 3 components'

        indexes, mapping = _find_indexes(self.mesh, self.lf_type,
                                         positions=self.positions,
                                         indexes=self.indexes,
                                         tissues=self.tissues,
                                         radius=self.radius)

        f = field[indexes]
        if self.directions is None:
            components = np.linalg.norm(f, axis=1)

        else:
            directions = _find_directions(self.mesh, self.lf_type,
                                          self.directions, indexes,
                                          mapping)

            components = np.sum(f * directions, axis=1)

        if self.lf_type == 'node':
            weights = self.mesh.nodes_volumes_or_areas()[indexes]
        elif self.lf_type == 'element':
            weights = self.mesh.elements_volumes_and_areas()[indexes]
        else:
            raise ValueError("lf_type must be 'node' or 'element'."
                             " Got: {0} instead".format(self.lf_type))

        return np.average(components, weights=weights)

    def mean_angle(self, field):
        ''' Calculates the mean angle between the field and the target

        Parameters
        -----------
        field: Nx3 NodeData or ElementData
            Electric field

        Returns
        ------------
        angle: float
            Mean angle in this target between the field and the target direction, in
            degrees
        '''
        if (self.positions is None) == (self.indexes is None):  # negative XOR operation
            raise ValueError('Please set either positions or indexes')

        assert self.mesh is not None, 'Please set a mesh'
        assert field.nr_comp == 3, 'Field must have 3 components'
        if self.directions is None:
            return np.nan

        indexes, mapping = _find_indexes(self.mesh, self.lf_type,
                                         positions=self.positions,
                                         indexes=self.indexes,
                                         tissues=self.tissues,
                                         radius=self.radius)

        directions = _find_directions(self.mesh, self.lf_type,
                                      self.directions, indexes,
                                      mapping)
        if self.intensity < 0:
            directions *= -1
        f = field[indexes]
        components = np.sum(f * directions, axis=1)
        norm = np.linalg.norm(f, axis=1)
        tangent = np.sqrt(norm ** 2 - components ** 2)
        angles = np.rad2deg(np.arctan2(tangent, components))
        if self.lf_type == 'node':
            weights = self.mesh.nodes_volumes_or_areas()[indexes]
        elif self.lf_type == 'element':
            weights = self.mesh.elements_volumes_and_areas()[indexes]
        else:
            raise ValueError("lf_type must be 'node' or 'element'."
                             " Got: {0} instead".format(self.lf_type))
        weights *= norm
        return np.average(angles, weights=weights)

    def __str__(self):
        s = ('positions: {0}\n'
             'indexes: {1}\n'
             'directions: {2}\n'
             'radius: {3}\n'
             'intensity: {4}\n'
             'max_angle: {5}\n'
             'tissues: {6}\n'
        .format(
            str(self.positions),
            str(self.indexes),
            str(self.directions),
            self.radius,
            self.intensity,
            str(self.max_angle),
            str(self.tissues)))
        return s


class TDCSavoid:
    ''' List of positions to be avoided by optimizer

    Attributes
    -------------
    positions: Nx3 ndarray
        List of positions to be avoided, in x, y, z coordinates and in the subject space.
        Will find the closest mesh points
    indexes: Nx1 ndarray of ints
        Indexes (1-based) of elements/nodes to be avoided. Overwrites positions
    weight : float (optional)
        Weight to give to avoid region. The larger, the more we try to avoid it. Default:
        1e3
    radius: float (optional)
        Radius of region. All the elements/nodes within the given radius of the indexes
        will be included.
    tissues: list or None (Optional)
        Tissues to be included in the region. Either a list of integer with tissue tags or None
        for all tissues. Default: None


    Note
    -------
    If both positions and indexes are set to None, and a tissue is set, it will set the
    given weith to all elements/nodes in the given tissues

    THE ONES BELLOW SHOULD NOT BE FILLED BY THE USERS IN NORMAL CIRCUNTANCES:

    mesh: simnibs.msh.mesh_io.Msh (optional)
        Mesh where the target is defined. Set by the TDCSoptimize methods
    lf_type: 'node' or 'element'
        Where the electric field values are defined

    Warning
    -----------
    Changing positions constructing the class
    can cause unexpected behaviour
    '''

    def __init__(self, positions=None, indexes=None,
                 weight=1e3, radius=2, tissues=None,
                 mesh=None, lf_type=None):
        self.lf_type = lf_type
        self.mesh = mesh
        self.radius = radius
        self.tissues = tissues
        self.positions = positions
        self.indexes = indexes
        self.weight = weight

    @classmethod
    def read_mat_struct(cls, mat):
        '''Reads a .mat structure

        Parameters
        -----------
        mat: dict
            Dictionary from scipy.io.loadmat

        Returns
        ----------
        t: TDCSavoid
            TDCSavoid structure
        '''
        t = cls()
        positions = try_to_read_matlab_field(mat, 'positions', list, t.positions)
        indexes = try_to_read_matlab_field(mat, 'indexes', list, t.indexes)
        weight = try_to_read_matlab_field(mat, 'weight', float, t.weight)
        radius = try_to_read_matlab_field(mat, 'radius', float, t.radius)
        tissues = try_to_read_matlab_field(mat, 'tissues', list, t.tissues)
        if positions is not None and len(positions) == 0:
            positions = None
        if indexes is not None and len(indexes) == 0:
            indexes = None
        if tissues is not None and len(tissues) == 0:
            tissues = None

        is_empty = True
        is_empty *= t.positions == positions
        is_empty *= t.indexes == indexes
        is_empty *= t.weight == weight
        is_empty *= t.tissues == tissues
        if is_empty:
            return None

        return cls(positions, indexes, weight, radius, tissues)

    def _get_avoid_region(self):
        if (self.indexes is not None) or (self.positions is not None):
            indexes, _ = _find_indexes(self.mesh, self.lf_type,
                                       positions=self.positions,
                                       indexes=self.indexes,
                                       tissues=self.tissues,
                                       radius=self.radius)
            return indexes
        elif self.tissues is not None:
            if self.lf_type == 'element':
                return self.mesh.elm.elm_number[
                    np.isin(self.mesh.elm.tag1, self.tissues)]
            elif self.lf_type == 'node':
                return self.mesh.elm.nodes_with_tag(self.tissues)
        else:
            raise ValueError('Please define either indexes/positions or tissues')

    def avoid_field(self):
        ''' Returns a field with self.weight in the target area and
        weight=1 outside the target area

        Returns
        ------------
        w: float, >= 1
            Weight field
        '''
        assert self.mesh is not None, 'Please set a mesh'
        assert self.lf_type is not None, 'Please set a lf_type'
        assert self.weight >= 0, 'Weights must be >= 0'
        if self.lf_type == 'node':
            f = np.ones(self.mesh.nodes.nr)
        elif self.lf_type == 'element':
            f = np.ones(self.mesh.elm.nr)
        else:
            raise ValueError("lf_type must be 'node' or 'element'."
                             " Got: {0} instead".format(self.lf_type))

        indexes = self._get_avoid_region()
        f[indexes - 1] = self.weight
        if len(indexes) == 0:
            raise ValueError('Empty avoid region!')

        return f

    def as_field(self, name='weights'):
        ''' Returns a NodeData or ElementData field with the weights

        Paramets
        ---------
        name: str (optional)
            Name for the field

        Returns
        --------
        f: NodeData or ElementData
            Field with weights
        '''
        w = self.avoid_field()
        if self.lf_type == 'node':
            return mesh_io.NodeData(w, name, mesh=self.mesh)
        elif self.lf_type == 'element':
            return mesh_io.ElementData(w, name, mesh=self.mesh)

    def mean_field_norm_in_region(self, field):
        ''' Calculates the mean field magnitude in the region defined by the avoid structure

        Parameters
        -----------
        field: ElementData or NodeData
            Field for which we calculate the mean magnitude
        '''
        assert self.mesh is not None, 'Please set a mesh'
        assert self.lf_type is not None, 'Please set a lf_type'
        indexes = self._get_avoid_region()
        v = np.linalg.norm(field[indexes], axis=1)
        if self.lf_type == 'node':
            weight = self.mesh.nodes_volumes_or_areas()[indexes]
        elif self.lf_type == 'element':
            weight = self.mesh.elements_volumes_and_areas()[indexes]
        else:
            raise ValueError("lf_type must be 'node' or 'element'."
                             " Got: {0} instead".format(self.lf_type))

        return np.average(v, weights=weight)

    def __str__(self):
        s = ('positions: {0}\n'
             'indexes: {1}\n'
             'radius: {2}\n'
             'weight: {3:.1e}\n'
             'tissues: {4}\n'
        .format(
            str(self.positions),
            str(self.indexes),
            self.radius,
            self.weight,
            str(self.tissues)))
        return s


class TDCSDistributedOptimize():
    ''' Defines a tdcs optimization problem with distributed sources

    This function uses the problem setup from

    Ruffini et al. "Optimization of multifocal transcranial current
    stimulation for weighted cortical pattern targeting from realistic modeling of
    electric fields", NeuroImage, 2014

    And the algorithm from

    Saturnino et al. "Accessibility of cortical regions to focal TES:
    Dependence on spatial position, safety, and practical constraints."
    NeuroImage, 2019

    Parameters
    --------------
    leadfield_hdf: str (optional)
        Name of file with leadfield
    max_total_current: float (optional)
        Maximum current across all electrodes (in Amperes). Default: 2e-3
    max_individual_current: float (optional)
        Maximum current for any single electrode (in Amperes). Default: 1e-3
    max_active_electrodes: int (optional)
        Maximum number of active electrodes. Default: no maximum
    name: str (optional)
        Name of optimization problem. Default: optimization
    target_image: str or pair (array, affine)
        Image to be "reproduced" via the optimization
    mni_space: bool (optional)
        Wether the image is in MNI space. Default True
    subpath: str (optional)
        Path to the subject "m2m" folder. Needed if mni_space=True
    intensity: float
        Target field intensity
    min_img_value: float >= 0 (optional)
        minimum image (for example t value) to be considered. Corresponds to T_min in
        Ruffini et al. 2014. Default: 0
    open_in_gmsh: bool (optional)
        Whether to open the result in Gmsh after the calculations. Default: False

    Attributes
    --------------
    leadfield_hdf: str
        Name of file with leadfield
    max_total_current: float (optional)
        Maximum current across all electrodes (in Amperes). Default: 2e-3
    max_individual_current: float
        Maximum current for any single electrode (in Amperes). Default: 1e-3
    max_active_electrodes: int
        Maximum number of active electrodes. Default: no maximum
    ledfield_path: str
        Path to the leadfield in the hdf5 file. Default: '/mesh_leadfield/leadfields/tdcs_leadfield'
    mesh_path: str
        Path to the mesh in the hdf5 file. Default: '/mesh_leadfield/'

    The two above are used to define:

    mesh: simnibs.msh.mesh_io.Msh
        Mesh with problem geometry

    leadfield: np.ndarray
        Leadfield matrix (N_elec -1 x M x 3) where M is either the number of nodes or the
        number of elements in the mesh. We assume that there is a reference electrode

    Alternatively, you can set the three attributes above and not leadfield_path,
    mesh_path and leadfield_hdf

    lf_type: None, 'node' or 'element'
        Type of leadfield.

    name: str
        Name for the optimization problem. Defaults tp 'optimization'

    target_image: str or pair (array, affine)
        Image to be "reproduced" via the optimization

    mni_space: bool (optional)
        Wether the image is in MNI space. Default True

    subpath: str (optional)
        Path to the subject "m2m" folder. Needed if mni_space=True

    intensity: float
        Target field intensity

    min_img_value: float >= 0 (optional)
        minimum image (for example t value) to be considered. Corresponds to T_min in
        Ruffini et al. 2014. Default: 0

    open_in_gmsh: bool (optional)
        Whether to open the result in Gmsh after the calculations. Default: False

    Warning
    -----------
    Changing leadfield_hdf, leadfield_path and mesh_path after constructing the class
    can cause unexpected behaviour
    '''

    def __init__(self, leadfield_hdf=None,
                 max_total_current=2e-3,
                 max_individual_current=1e-3,
                 max_active_electrodes=None,
                 name='optimization/tdcs',
                 target_image=None,
                 mni_space=True,
                 subpath=None,
                 intensity=0.2,
                 min_img_value=0,
                 open_in_gmsh=True):

        self._tdcs_opt_obj = TDCSoptimize(
            leadfield_hdf=leadfield_hdf,
            max_total_current=max_total_current,
            max_individual_current=max_individual_current,
            max_active_electrodes=max_active_electrodes,
            name=name,
            target=[],
            avoid=[],
            open_in_gmsh=open_in_gmsh
        )
        self.max_total_current = max_total_current
        self.max_individual_current = max_individual_current
        self.max_active_electrodes = max_active_electrodes
        self.leadfield_path = '/mesh_leadfield/leadfields/tdcs_leadfield'
        self.mesh_path = '/mesh_leadfield/'
        self.target_image = target_image
        self.mni_space = mni_space
        self.open_in_gmsh = open_in_gmsh
        self.subpath = subpath
        self.name = name

        self.intensity = intensity
        self.min_img_value = min_img_value

        if min_img_value < 0:
            raise ValueError('min_img_value must be > 0')

    @property
    def lf_type(self):
        self._tdcs_opt_obj.mesh = self.mesh
        self._tdcs_opt_obj.leadfield = self.leadfield

        return self._tdcs_opt_obj.lf_type

    @property
    def leadfield_hdf(self):
        return self._tdcs_opt_obj.leadfield_hdf

    @leadfield_hdf.setter
    def leadfield_hdf(self, leadfield_hdf):
        self._tdcs_opt_obj.leadfield_hdf = leadfield_hdf

    @property
    def leadfield_path(self):
        return self._tdcs_opt_obj.leadfield_path

    @leadfield_path.setter
    def leadfield_path(self, leadfield_path):
        self._tdcs_opt_obj.leadfield_path = leadfield_path

    @property
    def mesh_path(self):
        return self._tdcs_opt_obj.mesh_path

    @mesh_path.setter
    def mesh_path(self, mesh_path):
        self._tdcs_opt_obj.mesh_path = mesh_path

    @property
    def name(self):
        return self._tdcs_opt_obj.name

    @name.setter
    def name(self, name):
        self._tdcs_opt_obj.name = name

    @property
    def leadfield(self):
        ''' Reads the leadfield from the HDF5 file'''
        self._tdcs_opt_obj.leadfield_hdf = self.leadfield_hdf
        return self._tdcs_opt_obj.leadfield

    @leadfield.setter
    def leadfield(self, leadfield):
        self._tdcs_opt_obj.leadfield = leadfield

    @property
    def mesh(self):
        self._tdcs_opt_obj.leadfield_hdf = self.leadfield_hdf
        return self._tdcs_opt_obj.mesh

    @mesh.setter
    def mesh(self, mesh):
        self._tdcs_opt_obj.mesh = mesh

    @property
    def field_name(self):
        self._tdcs_opt_obj.leadfield_hdf = self.leadfield_hdf
        return self._tdcs_opt_obj._field_name

    @field_name.setter
    def field_name(self, field_name):
        self._tdcs_opt_obj._field_name = field_name

    @property
    def field_units(self):
        self._tdcs_opt_obj.leadfield_hdf = self.leadfield_hdf
        return self._tdcs_opt_obj._field_units

    def to_mat(self):
        """ Makes a dictionary for saving a matlab structure with scipy.io.savemat()

        Returns
        --------------------
        dict
            Dictionaty for usage with scipy.io.savemat
        """
        mat = {}
        mat['type'] = 'TDCSDistributedOptimize'
        mat['leadfield_hdf'] = remove_None(self.leadfield_hdf)
        mat['max_total_current'] = remove_None(self.max_total_current)
        mat['max_individual_current'] = remove_None(self.max_individual_current)
        mat['max_active_electrodes'] = remove_None(self.max_active_electrodes)
        mat['open_in_gmsh'] = remove_None(self.open_in_gmsh)
        mat['name'] = remove_None(self.name)
        mat['target_image'] = remove_None(self.target_image)
        mat['mni_space'] = remove_None(self.mni_space)
        mat['subpath'] = remove_None(self.subpath)
        mat['intensity'] = remove_None(self.intensity)
        mat['min_img_value'] = remove_None(self.min_img_value)

        return mat

    @classmethod
    def read_mat_struct(cls, mat):
        '''Reads a .mat structure

        Parameters
        -----------
        mat: dict
            Dictionary from scipy.io.loadmat

        Returns
        ----------
        p: TDCSoptimize
            TDCSoptimize structure
        '''
        t = cls()
        leadfield_hdf = try_to_read_matlab_field(
            mat, 'leadfield_hdf', str, t.leadfield_hdf)
        max_total_current = try_to_read_matlab_field(
            mat, 'max_total_current', float, t.max_total_current)
        max_individual_current = try_to_read_matlab_field(
            mat, 'max_individual_current', float, t.max_individual_current)
        max_active_electrodes = try_to_read_matlab_field(
            mat, 'max_active_electrodes', int, t.max_active_electrodes)
        open_in_gmsh = try_to_read_matlab_field(
            mat, 'open_in_gmsh', bool, t.open_in_gmsh)
        name = try_to_read_matlab_field(
            mat, 'name', str, t.name)
        target_image = try_to_read_matlab_field(
            mat, 'target_image', str, t.target_image)
        mni_space = try_to_read_matlab_field(
            mat, 'mni_space', bool, t.mni_space)
        subpath = try_to_read_matlab_field(
            mat, 'subpath', str, t.subpath)
        intensity = try_to_read_matlab_field(
            mat, 'intensity', float, t.intensity)
        min_img_value = try_to_read_matlab_field(
            mat, 'min_img_value', float, t.min_img_value)

        return cls(
            leadfield_hdf=leadfield_hdf,
            max_total_current=max_total_current,
            max_individual_current=max_individual_current,
            max_active_electrodes=max_active_electrodes,
            name=name,
            target_image=target_image,
            mni_space=mni_space,
            subpath=subpath,
            intensity=intensity,
            min_img_value=min_img_value,
            open_in_gmsh=open_in_gmsh
        )

    def _target_distribution(self):
        ''' Gets the y and W fields, by interpolating the target_image

        Based on Eq. 1 from
        Ruffini et al. "Optimization of multifocal transcranial current
        stimulation for weighted cortical pattern targeting from realistic modeling of
        electric fields", NeuroImage, 2014
        '''
        assert self.mesh is not None, 'Please set a mesh'
        assert self.min_img_value >= 0, 'min_img_value must be >= 0'
        assert self.intensity is not None, 'intensity not set'
        # load image
        if isinstance(self.target_image, str):
            img = nibabel.load(self.target_image)
            vol = np.array(img.dataobj)
            affine = img.affine
        else:
            vol, affine = self.target_image
        vol = vol.squeeze()  # fix when image is "4D", i.e. NxMxKx1
        if vol.ndim != 3:
            raise ValueError('Target image has to be 3D')
        vol[np.isnan(vol)] = 0.0

        # if in MNI space, tranfrom coordinates
        if self.mni_space:
            if self.subpath is None:
                raise ValueError('subpath not set!')
            nodes_mni = transformations.subject2mni_coords(
                self.mesh.nodes[:], self.subpath
            )
            orig_nodes = np.copy(self.mesh.nodes[:])
            self.mesh.nodes.node_coord = nodes_mni
        # Interpolate
        if self.lf_type == 'node':
            field = mesh_io.NodeData.from_data_grid(self.mesh, vol, affine)
        elif self.lf_type == 'element':
            field = mesh_io.ElementData.from_data_grid(self.mesh, vol, affine)
        field = np.float64(field[:])

        # setting values in eyes to zero
        if np.any(self.mesh.elm.tag1 == 1006):
            logger.info('setting target values in eyes to zero')
            if self.lf_type == 'node':
                eye_nodes = np.unique(self.mesh.elm.node_number_list[self.mesh.elm.tag1 == 1006, :])
                eye_nodes = eye_nodes[eye_nodes > 0]
                field[eye_nodes - 1] = 0.0  # node indices in mesh are 1-based
            elif self.lf_type == 'element':
                field[self.mesh.elm.tag1 == 1006] = 0.0

        if self.mni_space:
            self.mesh.nodes.node_coord = orig_nodes

        W = np.abs(field)
        W[np.abs(field) < self.min_img_value] = self.min_img_value
        y = field[:].copy()
        y[np.abs(field) < self.min_img_value] = 0
        y *= self.intensity

        if np.all(np.abs(field) < self.min_img_value):
            raise ValueError('Target image values are below min_img_value!')
        return y, W

    def normal_directions(self):
        assert self.mesh is not None, 'Please set a mesh'
        assert self.lf_type is not None, 'Please set a lf_type'

        if 4 in self.mesh.elm.elm_type:
            raise ValueError("Can't define a normal direction for volumetric data!")

        if self.lf_type == 'node':
            normals = self.mesh.nodes_normals()[:]
        elif self.lf_type == 'element':
            normals = self.mesh.triangle_normals()[:]

        return -normals

    def field(self, currents):
        ''' Outputs the electric fields caused by the current combination

        Parameters
        -----------
        currents: N_elec x 1 ndarray
            Currents going through each electrode, in A. Usually from the optimize
            method. The sum should be approximately zero

        Returns
        ----------
        E: simnibs.mesh.NodeData or simnibs.mesh.ElementData
            NodeData or ElementData with the field caused by the currents
        '''
        return self._tdcs_opt_obj.field(currents)

    def field_mesh(self, currents):
        ''' Creates showing the targets and the field
        Parameters
        -------------
        currents: N_elec x 1 ndarray
            Currents going through each electrode, in A. Usually from the optimize
            method. The sum should be approximately zero

        Returns
        ---------
        results: simnibs.msh.mesh_io.Msh
            Mesh file
        '''
        e_field = self.field(currents)
        e_magn_field = e_field.norm()
        normals = self.normal_directions()
        e_normal_field = np.sum(e_field[:] * normals, axis=1)
        target_map, W = self._target_distribution()
        erni = (target_map - W * e_normal_field) ** 2 - target_map ** 2
        erni *= len(target_map) / np.sum(W)

        m = copy.deepcopy(self.mesh)
        if self.lf_type == 'node':
            add_field = m.add_node_field
        elif self.lf_type == 'element':
            add_field = m.add_element_field

        add_field(e_field, e_field.field_name)
        add_field(e_magn_field, e_magn_field.field_name)
        add_field(e_normal_field, 'normal' + e_field.field_name)
        add_field(target_map, 'target_map')
        add_field(erni, 'ERNI')
        return m

    def optimize(self, fn_out_mesh=None, fn_out_csv=None):
        ''' Runs the optimization problem

        Parameters
        -------------
        fn_out_mesh: str
            If set, will write out the electric field and currents to the mesh

        fn_out_mesh: str
            If set, will write out the currents and electrode names to a CSV file


        Returns
        ------------
        currents: N_elec x 1 ndarray
            Optimized currents. The first value is the current in the reference electrode
        '''
        assert self.leadfield is not None, 'Leadfield not defined'
        assert self.mesh is not None, 'Mesh not defined'
        if self.max_active_electrodes is not None:
            assert self.max_active_electrodes > 1, \
                'The maximum number of active electrodes should be at least 2'

        if self.max_total_current is None:
            logger.warning('Maximum total current not set!')
            max_total_current = 1e3
        else:
            assert self.max_total_current > 0
            max_total_current = self.max_total_current

        if self.max_individual_current is None:
            max_individual_current = max_total_current

        else:
            assert self.max_individual_current > 0
            max_individual_current = self.max_individual_current

        assert self.min_img_value is not None, 'min_img_value not set'
        assert self.intensity is not None, 'intensity not set'

        y, W = self._target_distribution()
        normals = self.normal_directions()
        weights = np.sqrt(self._tdcs_opt_obj.get_weights())

        if self.max_active_electrodes is None:
            opt_problem = optimization_methods.TESDistributed(
                W[None, :, None] * self.leadfield,
                y[:, None] * normals, weights[:, None] * normals,
                max_total_current,
                max_individual_current
            )
        else:
            opt_problem = optimization_methods.TESDistributedElecConstrained(
                self.max_active_electrodes,
                W[None, :, None] * self.leadfield,
                y[:, None] * normals, weights[:, None] * normals,
                max_total_current,
                max_individual_current
            )

        currents = opt_problem.solve()

        logger.log(25, '\n' + self.summary(currents))

        if fn_out_mesh is not None:
            fn_out_mesh = os.path.abspath(fn_out_mesh)
            m = self.field_mesh(currents)
            m.write(fn_out_mesh)
            v = m.view()
            ## Configure view
            v.Mesh.SurfaceFaces = 0
            v.View[2].Visible = 1
            # Electrode geo file
            el_geo_fn = os.path.splitext(fn_out_mesh)[0] + '_el_currents.geo'
            self._tdcs_opt_obj.electrode_geo(el_geo_fn, currents)
            v.add_merge(el_geo_fn)
            max_c = np.max(np.abs(currents))
            v.add_view(Visible=1, RangeType=2,
                       ColorTable=gmsh_view._coolwarm_cm(),
                       CustomMax=max_c, CustomMin=-max_c)
            v.write_opt(fn_out_mesh)
            if self.open_in_gmsh:
                mesh_io.open_in_gmsh(fn_out_mesh, True)

        if fn_out_csv is not None:
            self._tdcs_opt_obj.write_currents_csv(currents, fn_out_csv)

        return currents

    def __str__(self):
        s = 'Optimization set-up\n'
        s += '===========================\n'
        s += 'Leadfield file: {0}\n'.format(self.leadfield_hdf)
        s += 'Max. total current: {0} (A)\n'.format(self.max_total_current)
        s += 'Max. individual current: {0} (A)\n'.format(self.max_individual_current)
        s += 'Max. active electrodes: {0}\n'.format(self.max_active_electrodes)
        s += 'Name: {0}\n'.format(self.name)
        s += '----------------------\n'
        s += 'Target image: {0}\n'.format(self.target_image)
        s += 'MNI space: {0}\n'.format(self.mni_space)
        s += 'Min. image value: {0}\n'.format(self.min_img_value)
        s += 'Target intensity: {0}\n'.format(self.intensity)
        return s

    def summary(self, currents):
        ''' Returns a string with a summary of the optimization

        Parameters
        ------------
        field: ElementData or NodeData
            Field of interest

        Returns
        ------------
        summary: str
            Summary of field
        '''
        s = self._tdcs_opt_obj.summary(currents)
        # Calculate erri
        field = self.field(currents)[:]
        normals = self.normal_directions()
        field_normal = np.sum(field * normals, axis=1)
        y, W = self._target_distribution()
        erri = np.sum((y - field_normal * W) ** 2 - y ** 2)
        erri *= len(y) / np.sum(W ** 2)
        # add Erri to messaga
        s += f'Error Relative to Non Intervention (ERNI): {erri:.2e}\n'
        return s

    def run(self, cpus=1):
        ''' Interface to use with the run_simnibs function

        Parameters
        ---------------
        cpus: int (optional)
            Does not do anything, it is just here for the common interface with the
            simulation's run function
        '''
        return TDCSoptimize.run(self)


def _save_TDCStarget_mat(target):
    target_dt = np.dtype(
        [('type', 'O'),
         ('indexes', 'O'), ('directions', 'O'),
         ('positions', 'O'), ('intensity', 'O'),
         ('max_angle', 'O'), ('radius', 'O'),
         ('tissues', 'O')])

    target_mat = np.empty(len(target), dtype=target_dt)

    for i, t in enumerate(target):
        target_mat[i] = np.array([
            ('TDCStarget',
             remove_None(t.indexes),
             remove_None(t.directions),
             remove_None(t.positions),
             remove_None(t.intensity),
             remove_None(t.max_angle),
             remove_None(t.radius),
             remove_None(t.tissues))],
            dtype=target_dt)

    return target_mat


def _save_TDCSavoid_mat(avoid):
    avoid_dt = np.dtype(
        [('type', 'O'),
         ('indexes', 'O'), ('positions', 'O'),
         ('weight', 'O'), ('radius', 'O'),
         ('tissues', 'O')])

    avoid_mat = np.empty(len(avoid), dtype=avoid_dt)

    for i, t in enumerate(avoid):
        avoid_mat[i] = np.array([
            ('TDCSavoid',
             remove_None(t.indexes),
             remove_None(t.positions),
             remove_None(t.weight),
             remove_None(t.radius),
             remove_None(t.tissues))],
            dtype=avoid_dt)

    return avoid_mat


def _find_indexes(mesh, lf_type, indexes=None, positions=None, tissues=None, radius=0.):
    ''' Looks into the mesh to find either
        1. nodes/elements withn a given radius of a set of points (defined as positions)
        and in the specified tissues. The fist step will be to find the closest
        node/element
        2. Specific indexes
    Returns the indices of the nodes/elements in the mesh as well as a mapping saying
    from which of the oridinal points the new points were acquired'''

    if (positions is not None) == (indexes is not None):  # negative XOR operation
        raise ValueError('Please define either positions or indexes')

    if indexes is not None:
        indexes = np.atleast_1d(indexes)
        return indexes, np.arange(len(indexes))

    if lf_type == 'node':
        if tissues is not None:
            mesh_indexes = mesh.elm.nodes_with_tag(tissues)
        else:
            mesh_indexes = mesh.nodes.node_number

        mesh_pos = mesh.nodes[mesh_indexes]

    elif lf_type == 'element':
        if tissues is not None:
            mesh_indexes = mesh.elm.elm_number[np.isin(mesh.elm.tag1, tissues)]
        else:
            mesh_indexes = mesh.elm.elm_number

        mesh_pos = mesh.elements_baricenters()[mesh_indexes]

    else:
        raise ValueError('lf_type must be either "node" or "element"')

    assert radius >= 0., 'radius should be >= 0'
    assert len(mesh_pos) > 0, 'Could not find any elements or nodes with given tags'
    kdtree = scipy.spatial.cKDTree(mesh_pos)
    pos_projected, indexes = kdtree.query(positions)
    indexes = np.atleast_1d(indexes)
    if radius > 1e-9:
        in_radius = kdtree.query_ball_point(mesh_pos[indexes], radius)
        original = np.concatenate([(i,) * len(ir) for i, ir in enumerate(in_radius)])
        in_radius, uq_idx = np.unique(np.concatenate(in_radius), return_index=True)
        return mesh_indexes[in_radius], original[uq_idx]
    else:
        return mesh_indexes[indexes], np.arange(len(indexes))


def _find_directions(mesh, lf_type, directions, indexes, mapping=None):
    if directions is None:
        return None
    if directions == 'normal':
        if 4 in np.unique(mesh.elm.elm_type):
            raise ValueError("Can't define a normal direction for volumetric data!")
        if lf_type == 'node':
            directions = -mesh.nodes_normals()[indexes]
        elif lf_type == 'element':
            directions = -mesh.triangle_normals()[indexes]
        return directions
    else:
        directions = np.atleast_2d(directions)
        if directions.shape[1] != 3:
            raise ValueError(
                "directions must be the string 'normal' or a Nx3 array"
            )
        if mapping is None:
            if len(directions) == len(indexes):
                mapping = np.arange(len(indexes))
            else:
                raise ValueError('Different number of indexes and directions and no '
                                 'mapping defined')
        elif len(directions) == 1:
            mapping = np.zeros(len(indexes), dtype=int)

        directions = directions / np.linalg.norm(directions, axis=1)[:, None]
        return directions[mapping]

