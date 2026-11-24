"""The global display preference used by the experimental Model Pictures renderer."""
from features import Setting

FLAG = 'HX_MODEL_PICTURES'
WEB = False
MENU = 'Displays'
ORDER = 120
SETTINGS = (Setting('enabled', b'Pictures\nModel', (b'On', b'Off'), default=1),)
