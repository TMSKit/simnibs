''' Example on how to run a SimNIBS TMS simulation using a coil with multiple stimulators in Python
'''
import os
from simnibs import sim_struct, run_simnibs

### General Information
S = sim_struct.SESSION()
S.subpath = 'm2m_ernie'  # m2m-folder of the subject
S.pathfem = 'tms_simu'  # Directory for the simulation

## Define the TMS simulation
tms = S.add_tmslist()
tms.fnamecoil = os.path.join('legacy_and_other','Magstim_70mm_Fig8.ccd')  # Choose a coil model

# Define the coil position
pos = tms.add_position()
pos.centre = 'C3'  # Place the coil over C3
pos.pos_ydir = 'CP3'  # Polongation of coil handle (see documentation)
#The first stimulator is connected to the figure of 8 part of the coil, the second one to the circular part
pos.didt = [10e6, 1e6]

pos_2 = tms.add_position()
pos_2.centre = 'C3'  
pos_2.pos_ydir = 'CP3'  
pos_2.didt = [10e6, 10e6]

pos_3 = tms.add_position()
pos_3.centre = 'C3'  
pos_3.pos_ydir = 'CP3'  
pos_3.didt = [1e6, 10e6]

# Run Simulation
run_simnibs(S)
