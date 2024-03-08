import sys
import os
import subprocess
from simnibs.GUI.main_gui import start_gui

big_sur = False
if sys.platform == 'darwin':
    try:
        mac_vers=subprocess.check_output('sw_vers -productVersion', shell=True)
        if int(mac_vers.split(b'.')[0]) >= 11:
            big_sur = True
    except:
        print('Mac OS sw_vers failed.')
if big_sur:
    print('Mac OS X Big Sur detected, setting QT_MAC_WANTS_LAYER=1 flag.')
    os.environ['QT_MAC_WANTS_LAYER'] = '1'

def main():
    start_gui(sys.argv)

if __name__ == '__main__':
    main()
