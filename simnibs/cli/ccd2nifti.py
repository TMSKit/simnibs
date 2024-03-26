# -*- coding: utf-8 -*-
'''
    command line tool to convert coil dipole definition ccd files to nifti1
    format. This program is part of the SimNIBS package.
    Please check on www.simnibs.org how to cite our work in publications.

    Copyright (C) 2021  Kristoffer H. Madsen

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

import os
import glob
import re
import numpy as np
import fmm3dpy
import nibabel as nib
import time


def read_ccl(fn):
    """ reads a ccl file, this format is similar to the ccd format. However,
    only line segments positions are included (first 3 columns) and an optional
    weighting in the forth column

    Parameters
    -----------
    fn: str
        name of ccl file

    Returns
    ----------
    [pos, m]: list
        positions of line segments
    """
    ccl_file = np.loadtxt(fn, skiprows=2)

    # if there is only 1 dipole, loadtxt return as array of the wrong shape
    if (len(np.shape(ccl_file)) == 1):
        a = np.zeros([1, 4])
        a[0, 0:3] = ccl_file[0:3]
        a[0, 3:] = ccl_file[3:]
        ccd_file = a

    return ccd_file[:, 0:3], ccd_file[:, 3:]

def read_ccd(fn):
    """ reads a ccd file

    Parameters
    -----------
    fn: str
        name of ccd file

    Returns
    ----------
    [pos, m]: list
        position and moment of dipoles
    """
    ccd_file = np.loadtxt(fn, skiprows=2)

    # if there is only 1 dipole, loadtxt return as array of the wrong shape
    if (len(np.shape(ccd_file)) == 1):
        a = np.zeros([1, 6])
        a[0, 0:3] = ccd_file[0:3]
        a[0, 3:] = ccd_file[3:]
        ccd_file = a

    return ccd_file[:, 0:3], ccd_file[:, 3:]

def parseccd(ccd_file):
    '''
    Parse ccd file, and return intended bounding box, resolution and dIdtmax
        and other fields if available

    Parameters
    ----------
    ccd_file : string
        ccd file to parse

    On ccd file format version 1.1:
    1) First line is a header line escaped with # which can contain any number
       of variables in the form variable=value, a few of these variables are
       reserved as can be seen below, they are separated by semicolons (;).
    2) The second line is the contains the number of dipoles expected
       (this is actually not used in practice).
    3) Third line contains a header text excaped by #, typically:
       # centers and weighted directions of the elements (magnetic dipoles)
    4-end) Remaining lines are space separated dipole positions and dipole
       moments in a string number format readable by numpy. E.g. each line must contain
       six values: x y z mx my mz, where the first three are x,y and positions
       in meters, and the remaining three are dipole moments in x,y and z direction
       in Coulumb * meter per 1 A/s input current.
       an example could be:
       0 0 0 0 0 1.0e-03
       indicating a dipole at position 0,0,0 in z direction with strength
       0.001 C*m*s/A

    The variables are used to encode additonal optional information in text, specifically:
    dIdtmax=147,100
        which would indicate a max dI/dt (at 100% MSO) of 146.9 A/microsecond
        for first stimulator and 100 A/microsecond for the second stimulator.
    dIdtstim=162
        Indicating the max dI/dt reported on the stimulation display of 162,
        typically this is used to create a rescaled version of the ccd file
        such that the stimulator reported dI/dt max can be used directly.
        This is currently only supported for one stimulator.
    stimulator=Model name 1,Model name 2
    brand=Brand name 1,Brand name 2
    coilname=name of coil
        Indicates the name of the coil for display purposes
    Some variables are used for expansion in to nifti1 format:
    x=-300,300
        Indicates that the ccd file should be expanded into a FOV from
        x=-300mm to x=300mm, this could also be indicated as x=300
    y=-300,300
        The same for y
    z=-200,200
        The same for z
    resolution=3,3,3
        Indicates that the resolution should be 3mm in x,y and z directions,
        this could also be given as resolution=3

    The below is an example header line:
    #Test CCD file;dIdtmax=162;x=300;y=300;z=200;resolution=3;stimulator=MagProX100;brand=MagVenture;


    '''
    def parseField(info, field):
        try:
            a = np.fromstring(info[field],sep=',')
        except:
            a = None
        return a
    if os.path.splitext(ccd_file)[1]=='.ccl':
        d_position, d_moment = read_ccl(ccd_file)
    else:
        d_position, d_moment = read_ccd(ccd_file)

    #reopen to read header
    f = open(ccd_file,'r')
    data = f.readline()
    fields = re.findall(r'(\w*=[^;]*)', data + ';')
    labels = [f.split('=')[0] for f in fields]
    values = [f.split('=')[1].rstrip('\n') for f in fields]
    info = {}
    for i,label in enumerate(labels):
        info[label]=values[i]

    #parse bounding box for nii
    bb = []
    for dim in ('x','y','z'):
        a = parseField(info, dim)
        if a is None:
            bb.append(None)
        else:
            if len(a)<2:
                bb.append((-np.abs(a),np.abs(a)))
            else:
                bb.append(a)

    #parse resolution
    res = []
    a = parseField(info, 'resolution')
    if a is None:
        res.append(None)
    else:
        if len(a)<3:
            for i in range(len(a),3):
                a = np.concatenate((a, (a[i-1],)))
        res = a
    return d_position, d_moment, bb, res, info

def A_from_dipoles(d_moment, d_position, target_positions, eps=1e-3, direct='auto'):
    '''
    Get A field from dipoles using FMM3D

    Parameters
    ----------
    d_moment : ndarray
        dipole moments (Nx3).
    d_position : ndarray
        dipole positions (Nx3).
    target_positions : ndarray
        positions for which to calculate the A field.
    eps : float
        Precision. The default is 1e-3
    direct : bool
        Set to true to force using direct (naive) approach or False to force use of FMM.
        If set to auto direct method is used for less than 300 dipoles which appears to be faster in these cases.
        The default is 'auto'

    Returns
    -------
    A : ndarray
        A field at points (M x 3) in Tesla*meter.

    '''
    #if set to auto use direct methods if # dipoles less than 300
    if direct=='auto':
        if d_moment.shape[0]<300:
            direct = True
        else:
            direct = False
    if direct is True:
        out = fmm3dpy.l3ddir(charges=d_moment.T, sources=d_position.T,
                  targets=target_positions.T, nd=3, pgt=2)
    elif direct is False:
        #use fmm3dpy to calculate expansion fast
        out = fmm3dpy.lfmm3d(charges=d_moment.T, eps=eps, sources=d_position.T,
                  targets=target_positions.T, nd=3, pgt=2)
    else:
        print('Error: direct flag needs to be either "auto", True or False')
    A = np.empty((target_positions.shape[0], 3), dtype=float)
    #calculate curl
    A[:, 0] = (out.gradtarg[1][2] - out.gradtarg[2][1])
    A[:, 1] = (out.gradtarg[2][0] - out.gradtarg[0][2])
    A[:, 2] = (out.gradtarg[0][1] - out.gradtarg[1][0])
    #scale
    A *= -1e-7
    return A

def B_from_dipoles(d_moment, d_position, target_positions, eps=1e-3, direct='auto'):
    '''
    Get B field from dipoles using FMM3D

    Parameters
    ----------
    d_moment : ndarray
        dipole moments (Nx3).
    d_position : ndarray
        dipole positions (Nx3).
    target_positions : ndarray
        position for which to calculate the B field.
    eps : float
        Precision. The default is 1e-3
    direct : bool
        Set to true to force using direct (naive) approach or False to force use of FMM.
        If set to auto direct method is used for less than 300 dipoles which appears to be faster i these cases.
        The default is 'auto'

    Returns
    -------
    B : ndarray
        B field at points (M x 3) in Tesla.

    '''
    #if set to auto use direct methods if # dipoles less than 300
    if direct=='auto':
        if d_moment.shape[0]<300:
            direct = True
        else:
            direct = False
    if direct is True:
        out = fmm3dpy.l3ddir(dipvec=d_moment.T, sources=d_position.T,
                  targets=target_positions.T, nd=1, pgt=2)
    elif direct is False:
        out = fmm3dpy.lfmm3d(dipvec=d_moment.T, eps=eps, sources=d_position.T,
                  targets=target_positions.T, nd=1, pgt=2)
    else:
        print('Error: direct flag needs to be either "auto", True or False')
    B = out.gradtarg.T
    B *= -1e-7
    return B


def writeccd(fn, mpos, m, info=None, extra=None):
    N=m.shape[0]
    f=open(fn,'w')
    f.write('# %s version 1.0;'%os.path.split(fn)[1])
    f.write('# %s version 1.1;'%os.path.split(fn)[1])
    if not info is None:
        for i,key in enumerate(info.keys()):
            f.write(f'{key}={info[key]};')
    if not extra is None:
	    f.write(f'{extra};')
    f.write('\n')
    f.write('%i\n'%N)
    f.write('# centers and weighted directions of the elements (magnetic dipoles)\n')
    for i in range(N):
        f.write('%.15e %.15e %.15e '%tuple(mpos[i]))
        f.write('%.15e %.15e %.15e\n'%tuple(m[i]))
    f.close()

def rescale_ccd(ccd_file, outname=None):
    d_position, d_moment, bb, res, info = parseccd(ccd_file)
    try:
        scale = np.fromstring(info['dIdtmax'],sep=',')[0] / \
            np.fromstring(info['dIdtstim'],sep=',')[0]
    except:
        raise ValueError('cannot find dIdtstim and/or dIdtmax field(s) in ccd file')
    #rescale dipole moment
    d_moment *= scale
    #set maxdIdt and remove dIdtstim
    info['dIdtmax'] = info['dIdtstim']
    info.pop('dIdtstim')

    if outname is None:
        inname = os.path.split(ccd_file)
        outname = os.path.join(inname[0],
                               os.path.splitext(inname[1])[0] +
                               '_rescaled' + '.ccd')
    writeccd(outname, d_position, d_moment, info)

def ccd2nifti(ccdfn, info={}, eps=1e-3, Bfield=False):
    '''
    Convert CCD coil dipole files to nifti1 format

    Parameters
    ----------
    ccdfn : string
        CCD file.
    resolution : ndarray, optional
        Resolution (dx,dy,dz). The default is (3,3,3).
    boundingbox : ndarray, optional
        Bounding box ((xmin,xmax),(ymin,ymax),(zmin,zmax)).
        The default is ((-300, 300), (-200, 200), (0, 300)).
    dIdt : float, optional
        Maximum dIdt. The default is None.
    eps : float, optional
        precision for fmm3dpy. The default is 1e-3.

    Returns
    -------
    nii : Nifti1Volume
        Nifti1Volume containing dA/dt field.

    '''
    #read and parse ccd file
    d_position, d_moment, boundingbox, resolution, info = parseccd(ccdfn)
    if not boundingbox[0] is None:
        bb = boundingbox
    else:
	    bb = np.array(((-300, 300), (-200, 200), (0, 300)))
    if not resolution[0] is None:
        res = resolution
    else:
        res = np.array((3., 3., 3.))
    #create grid
    dx = np.spacing(1e4)
    x = np.arange(bb[0][0], bb[0][1] + dx, res[0]) #xgrid
    y = np.arange(bb[1][0], bb[1][1] + dx, res[1]) #ygrid
    z = np.arange(bb[2][0], bb[2][1] + dx, res[2]) #zgrid
    xyz = np.meshgrid(x, y, z, indexing='ij') #grid
    #reshape to 2D
    xyz = np.array(xyz).reshape((3, len(x) * len(y) * len(z)))
    xyz *= 1.0e-3 #from mm to SI (meters)
    if Bfield:
        A = B_from_dipoles(d_moment, d_position, xyz.T)
    else:
        A = A_from_dipoles(d_moment, d_position, xyz.T)
    A = A.reshape((len(x), len(y), len(z), 3))
    #header info
    hdr = nib.Nifti1Header()
    hdr.set_data_dtype(np.float32)
    hdr.set_xyzt_units('mm','unknown')
    #affine matrix
    M = np.identity(4) * np.array((res[0], res[1], res[2], 1))
    M[0, 3] = x[0]
    M[1, 3] = y[0]
    M[2, 3] = z[0]
    #create nifti1 volume
    nii = nib.Nifti1Image(A, M, hdr)
    #set dIdtmax if availible
    try:
        dstr = f"dIdtmax={info['dIdtmax']}"
        try:
            dstr += f";coilname={info['coilname']}"
        except:
            pass
        try:
            dstr += f";stimulator={info['stimulator']}"
        except:
            pass
        try:
            dstr += f";brand={info['brand']}";
        except:
            pass
        nii.header['descrip'] = dstr
    except:
        print('no information on dIdtmax found omitting from nii file.')
    return nii


def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Convert CCD files to Nifti1 format')
    parser.add_argument('-i', '--infile', dest='infile', default=None, required=True,
                        help='CCD file to convert')
    parser.add_argument('-o', '--outfile', dest='outfile', default=None,
                    help='output filename, will default to replacing extension with .nii.gz')
    parser.add_argument('-r', '--rescale', dest='rescale', action='store_true',
                        help='Rescale CCD file according to stimulator reported'
                           'dI/dt (writes new ccd file - with suffix _rescaled)')
    parser.add_argument('-f', '--force', dest='force', action='store_true',
                        help='Force rewrite')
    parser.add_argument('-b', '--bfield', dest='Bfield', action='store_true',
                        help='Write B field instead of A field')

    options = parser.parse_args(sys.argv[1:])
    if os.path.isdir(options.infile):
        print(f'recursively processing CCD files in {options.infile}')
        ccd_files = glob.iglob(os.path.join(options.infile, '**', '*.ccd'),
                          recursive=True)
        options.outfile = None
    elif os.path.isfile(options.infile):
        ccd_files = (options.infile,)
    else:
        print(f'Cannot locate input file: {options.infile}')
    for ccdfile in ccd_files:
        if options.rescale:
            try:
                rescale_ccd(ccdfile,options.outfile)
                print(f'Successfully rescaled {ccdfile}')
            except:
                print(f'Rescaling {ccdfile} failed, check if dIdtstim exists'
                      'and that only one stimulator value is present')
        else:
            if options.outfile is None:
                outfile = os.path.splitext(ccdfile)[0] + '.nii.gz'
            else:
                outfile=options.outfile
            if len(glob.glob(os.path.splitext(outfile)[0] + '*')) == 0 or options.force:
                t0 = time.perf_counter()
                print(f'expanding CCD file {ccdfile}')
                nii = ccd2nifti(ccdfile, Bfield=options.Bfield)
                nii.to_filename(outfile)
                print(f'Time spend: {time.perf_counter()-t0:.0f}s')
            else:
                print(f'Nifti1 version of {ccdfile} already exists')

if __name__ == '__main__':
    main()
