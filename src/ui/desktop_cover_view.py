from gi.repository import Gtk, Adw, GObject

from ui.utils import AsyncPicture
from ui.widgets.visualizer import Visualizer


class DesktopCoverView(Adw.Bin):
    """Full-window "cover art" view for desktop. Intentionally minimal:
    the queue lives in the right-side OverlaySplitView sidebar, and
    every transport control / like button / title & artist label lives
    in the persistent player bar — so this view is just a big cover
    plus a small audio visualizer underneath.

    Built on Adw.ToolbarView so the page has an opaque background
    without hand-rolled CSS. A plain Gtk.Box with a ``background-color``
    rule was rendering transparently during the OVER_UP slide,
    revealing the browser content behind it.
    """

    __gsignals__ = {
        "dismiss": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, player):
        super().__init__()
        self.player = player

        toolbar = Adw.ToolbarView()
        toolbar.set_hexpand(True)
        toolbar.set_vexpand(True)
        self.set_child(toolbar)

        # Cover art. AspectFrame + obey_child=False keeps it square
        # regardless of the window's aspect ratio. halign/valign must
        # stay at FILL (the default) — using CENTER here made the frame
        # shrink to its child's *natural* size, which for low-res video
        # thumbnails (120×90, 320×180) capped the cover at that tiny
        # size instead of upscaling into the whole available area.
        self.cover_img = AsyncPicture(crop_to_square=True, player=self.player)
        self.cover_img.add_css_class("rounded")
        self.cover_img.set_content_fit(Gtk.ContentFit.COVER)
        self.cover_img.set_hexpand(True)
        self.cover_img.set_vexpand(True)

        cover_frame = Gtk.AspectFrame(ratio=1.0, obey_child=False)
        cover_frame.set_vexpand(True)
        cover_frame.set_hexpand(True)
        cover_frame.set_overflow(Gtk.Overflow.HIDDEN)
        cover_frame.set_child(self.cover_img)

        # Bar visualizer beneath the cover — narrows to the cover's
        # width via the same clamp so it never sticks out into the
        # wide-screen margins.
        self.visualizer = Visualizer(self.player, height=80)
        self.visualizer.add_css_class("cover-visualizer")
        self.visualizer.set_hexpand(True)

        # The cover is the elastic part of the stack: it gets whatever
        # height is left after the visualizer (fixed) and margins, and
        # AspectFrame keeps it square within that allocation. We must NOT
        # set valign=CENTER on the column — that forces the column to its
        # natural height (cover + visualizer), which on shorter windows
        # pushes the visualizer off the bottom of the screen.
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        column.set_hexpand(True)
        column.set_vexpand(True)
        column.set_margin_top(32)
        column.set_margin_bottom(32)
        column.set_margin_start(48)
        column.set_margin_end(48)
        column.append(cover_frame)
        column.append(self.visualizer)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(800)
        clamp.set_child(column)

        toolbar.set_content(clamp)

        # Keep the cover in sync with the currently-playing track.
        self.player.connect("metadata-changed", self._on_metadata_changed)

    def _on_metadata_changed(self, player, title, artist, thumb_url,
                             video_id, like_status):
        if thumb_url:
            self.cover_img.video_id = video_id
            self.cover_img.load_url(thumb_url)
        else:
            self.cover_img.set_paintable(None)
