import os
import sys

# Point libgphoto2 to bundled camera/IO driver plugins
if getattr(sys, '_MEIPASS', None):
    base = sys._MEIPASS
else:
    base = os.path.dirname(os.path.abspath(__file__))

os.environ['CAMLIBS'] = os.path.join(base, 'libgphoto2', '2.5.33')
os.environ['IOLIBS'] = os.path.join(base, 'libgphoto2_port', '0.12.2')
