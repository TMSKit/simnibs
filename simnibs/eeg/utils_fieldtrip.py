from pathlib import Path
from typing import Union

from nibabel.affines import apply_affine
import numpy as np
from scipy.io import loadmat

import simnibs
from simnibs.utils.csv_reader import write_csv_positions
from simnibs.utils.file_finder import SubjectFiles
from simnibs.utils.transformations import make_cross_subject_morph

from simnibs.mesh_tools.mesh_io import load_subject_surfaces

# EEG MONTAGE

to_m = dict(m=1, cm=1e-2, mm=1e-3)
from_m = {k: 1 / v for k, v in to_m.items()}


# SOURCE SPACE


def setup_source_space(
    m2m_dir: Union[Path, str],
    subsampling: Union[None, int] = None,
    morph_to_fsaverage: Union[None, int] = 10,
):
    """Setup a source space for use with FieldTrip.

    PARAMETERS
    ----------
    m2m_dir : Path-like
        The directory containing the segmentation and head model results.
    subsampling : None | int
        The subsampling to use (default = None).
    morph_to_fsaverage : None | int
        Whether or not to create a mapping from subject space to the fsaverage
    template (default = 10, which constructs a morph to the fsaverage 10k
        model).

    RETURNS
    -------
    src_from : dict
        Dictionary with the source model information.
    mmaps : dict | None
        Dictionary with scipy.sparse.csr_matrix describing the morph from
        subject to fsaverage.
    """

    m2m = SubjectFiles(subpath=str(m2m_dir))
    src_from = load_subject_surfaces(m2m, "central", subsampling)
    # if surface is subsampled, add normals from original surface
    normals = (
        {
            h: np.loadtxt(m2m.get_morph_data(h, "normals.csv", subsampling), delimiter=",")
            for h in src_from
        }
        if subsampling
        else None
    )
    src_from = make_sourcemodel(src_from, normals)

    if morph_to_fsaverage:
        morphs = make_cross_subject_morph(
            m2m, "fsaverage", subsampling, morph_to_fsaverage
        )
        mmaps = {h: v.morph_mat for h, v in morphs.items()}
    else:
        mmaps = None
    return src_from, mmaps


def make_sourcemodel(src: dict, normals: Union[dict, None] = None):
    """Create a dictionary with source model information which can be used with
    FieldTrip.

    PARAMETERS
    ----------
    src : dict
        Dictionary with entries `lh` and `rh` each being a
    normals : dict


    RETURNS
    -------
    sourcemodel : dict
        Dictionary with source model information.
    """
    hemi2fieldtrip = dict(lh="CORTEX_LEFT", rh="CORTEX_RIGHT")

    # Construct a composite triangulation for both hemispheres
    cortex = src["lh"].join_mesh(src["rh"])
    pos = cortex.nodes.node_coord
    # Msh class already uses 1-indexing (!) so no need to modify
    tri = cortex.elm.node_number_list[:, :3]

    # All sources are valid
    inside = np.ones(cortex.nodes.nr, dtype=bool)[:, None]

    # ft_read_headshape outputs this structure
    brainstructure = np.concatenate(
        [np.full(src[h].nodes.nr, i) for i, h in enumerate(src, start=1)]
    )
    brainstructurelabel = np.stack([hemi2fieldtrip[h] for h in src]).astype(object)

    sourcemodel = dict(
        pos=pos,
        tri=tri,
        unit="mm",
        inside=inside,
        brainstructure=brainstructure,
        brainstructurelabel=brainstructurelabel,
    )

    if normals:
        sourcemodel["normals"] = np.concatenate([normals[h] for h in src])

    return sourcemodel


# FORWARD


def make_forward(forward: dict, src: dict):
    """Make a forward dictionary in a FieldTrip compatible format from a
    dictionary with forward information.

    PARAMETERS
    ----------
    forward : dict
        Dictionary with forward solution information (as returned by
        `eeg.forward.prepare_forward`).
    src : dict
        Dictionary with source space information (as returned by
        `setup_source_space`).

    RETURNS
    -------
    fwd : dict
        The forward dictionary.

    NOTES
    -----
    The leadfield of a particular electrode may be plotted in FieldTrip like so

        fwd_mat = cell2mat(fwd.leadfield);
        elec = 10; % electrode
        ori = 2;  % orientation (1,2,3 corresponding to x,y,z)
        ft_plot_mesh(src, 'vertexcolor', fwd_mat(elec, ori:3:end)');
    """
    # Create a cell array of matrices by filling a numpy object. Each cell is
    # [n_channels, n_orientations]
    fwd_cell = np.empty(forward["n_sources"], dtype=object)
    for i in range(forward["n_sources"]):
        fwd_cell[i] = forward["data"][:, i]

    labels = np.array(forward["ch_names"]).astype(object)[:, None]

    # The forward structure of FieldTrip
    return dict(
        pos=src["pos"],
        inside=src["inside"],
        unit=src["unit"],
        leadfield=fwd_cell,
        label=labels,
        leadfielddimord=r"{pos}_chan_ori",
        cfg=f"Created by SimNIBS {simnibs.__version__}",
    )



def prepare_montage(
    fname_montage: Union[Path, str],
    fname_info: Union[Path, str],
    fname_trans: Union[None, Path, str] = None,
):
    """Prepare SimNIBS montage file from a FieldTrip data structure containing
    an `elec` field describing the electrode configuration.

    PARAMETERS
    ----------
    fname_montage :
        Name of the SimNIBS montage file to write.
    fname_info :
        Name of a MAT file containing the electrode information in the field
        `elec`.
    fname_trans :
        Affine transformation matrix to apply to the electrode positions before
        writing to `fname_montage`.

    RETURNS
    -------
    """
    fname_info = Path(fname_info)

    info = loadmat(fname_info)["elec"][0]
    info = dict(zip(info.dtype.names, info[0]))
    info["label"] = np.array([label[0] for label in info["label"].squeeze()])
    info["chantype"] = np.array([ct[0] for ct in info["chantype"].squeeze()])
    info["unit"] = info["unit"][0]
    scale = to_m[info["unit"]] * from_m["mm"]
    info["elecpos"] *= scale
    # info["chanpos"] *= scale  # unused

    if fname_trans:
        fname_trans = Path(fname_trans)
        if fname_trans.suffix == ".mat":
            trans = loadmat(fname_trans)["trans"]
        elif fname_trans.suffix == ".txt":
            trans = np.loadtxt(fname_trans)
        else:
            raise ValueError("`fname_trans` must be either a MAT or a TXT file.")

        info["elecpos"] = apply_affine(trans, info["elecpos"])
        # info["chanpos"] = apply_affine(trans, info["chanpos"])

    is_eeg = info["chantype"] == "eeg"

    write_csv_positions(
        fname_montage,
        ["Electrode"] * sum(is_eeg),
        info["elecpos"][is_eeg],
        info["label"][is_eeg].tolist(),
    )
