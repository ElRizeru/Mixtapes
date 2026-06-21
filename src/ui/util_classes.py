from gi.repository import Gtk
from ui.utils import suppress_hover_while_scrolling


class ScrolledWindow(Gtk.ScrolledWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Suppress hover-background fades while scrolling — they cause stutter
        # when the pointer sits over rows that slide past it.
        suppress_hover_while_scrolling(super())
