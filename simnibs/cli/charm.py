# -*- coding: utf-8 -*-
'''
    command line tool to create tetrahedral head meshes from MR images
    This program is part of the SimNIBS package.
    Please check on www.simnibs.org how to cite our work in publications.

    Copyright (C) 2020  Oula Puonti, Guilherme B Saturnino, Jesper D Nielsen,
    Fang Cao, Axel Thielscher

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>

'''

import argparse
import os
import shutil
import sys
import textwrap
import time

from simnibs.segmentation import charm_main
from simnibs.cli.utils.helpers import add_argument
from simnibs.cli.utils import args_general, args_charm


def parseArguments(argv):

    usage_text = textwrap.dedent('''

CREATE HEAD MESH:
    charm subID T1 {T2}

VISUAL CHECK OF RESULTS:
    open the m2m_{subID}/results.html

RUN ONLY PARTS OF CHARM:
    charm subID T1 T2 --registerT2  (registration of T2 to T1)
    charm subID {T1} --initatlas  (initial affine registration of atlas to MR images)
    charm subID --segment  (make label image, reconstruct surfaces, register to fsaverage and MNI)
    charm subID --mesh  (create head mesh from label images)

    Note: Parts can be concatenated, e.g. charm subID --initatlas --segment

MANUAL EDITING:
    edit m2m_{subID}/label_prep/tissue_labeling_upsampled.nii.gz using a
    viewer of your choice, then call charm subID --mesh to recreate head mesh

    ''')

    parser = argparse.ArgumentParser(prog="charm", usage=usage_text)

    add_argument(parser, args_charm.subid) # NB
    add_argument(parser, args_general.version)
    add_argument(parser, args_charm.primary_image)
    add_argument(parser, args_charm.secondary_image)
    add_argument(parser, args_charm.register_t2)
    add_argument(parser, args_charm.init_atlas)
    add_argument(parser, args_charm.segment)
    add_argument(parser, args_charm.mesh)
    add_argument(parser, args_charm.surfaces)
    add_argument(parser, args_charm.forcerun)
    add_argument(parser, args_charm.skip_register_t2)
    add_argument(parser, args_charm.use_settings)
    add_argument(parser, args_charm.no_neck)
    add_argument(parser, args_charm.init_transform)
    add_argument(parser, args_charm.force_qform)
    add_argument(parser, args_charm.force_sform)
    add_argument(parser, args_charm.use_transform)
    add_argument(parser, args_charm.fs_subjects_dir)
    add_argument(parser, args_general.debug)

    args = parser.parse_args(argv)

    # subID is required, otherwise print help and exit (-v and -h handled by parser)
    if args.subID is None:
        parser.print_help()
        exit()

    return args


def main():
    args = parseArguments(sys.argv[1:])
    subject_dir = os.path.join(os.getcwd(), "m2m_"+args.subID)

    # run segmentation and meshing

    # check whether it's a fresh run
    fresh_run = args.registerT2
    fresh_run |= args.initatlas and not args.registerT2 and args.T1 is not None # initatlas is the first step in the pipeline when a T1 is explicitly supplied

    if not any([args.registerT2, args.initatlas, args.segment, args.mesh, args.surfaces]):
        # if charm part is not explicitly stated, run all
        fresh_run=True
        args.initatlas=True
        args.segment=True
        args.mesh=True
        args.surfaces = True
        if args.T2 is not None:
            args.registerT2=True

    # T1 name has to be supplied when it's a fresh run
    if fresh_run and args.T1 is None:
        raise RuntimeError("ERROR: Filename of T1-weighted image has to be supplied")

    # T2 name has to be supplied when registerT2==True
    if args.registerT2 and args.T2 is None:
        raise RuntimeError("ERROR: Filename of T2-weighted image has to be supplied")

    if fresh_run and os.path.exists(subject_dir):
        # stop when subject_dir folder exists and it's a fresh run (unless --forcerun is set)
        if not args.forcerun:
            raise RuntimeError("ERROR: --forcerun has to be set to overwrite existing m2m_{subID} folder")
        else:
            if args.usesettings is not None and os.path.dirname(os.path.abspath(args.usesettings[0])) == os.path.abspath(subject_dir):
                raise RuntimeError("ERROR: move the custom settings file out of the m2m-folder before running with --forcerun.")

            shutil.rmtree(subject_dir)
            time.sleep(2)


    charm_main.run(subject_dir, args.T1, args.T2, args.registerT2, args.initatlas,
                   args.segment, args.surfaces, args.mesh, args.usesettings, args.noneck,
                   args.inittransform, args.usetransform, args.forceqform, args.forcesform, args.fs_dir,
                   " ".join(sys.argv[1:]), args.debug)

    # mesh vs mesh_image
    # options_str = " ".join(sys.argv[1:])
    # charm_main.run(**args.__dict__, options_str=options_str)

if __name__ == '__main__':
    main()
