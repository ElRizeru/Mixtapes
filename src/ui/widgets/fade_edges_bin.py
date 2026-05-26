"""FadeEdgesBin: a Gtk.Box that masks its content with a vertical
gradient so the top and bottom edges fade out to transparent. Used by
the lyrics view so the scrolling lines dissolve into the background
instead of hard-cropping at the chrome boundary.

GTK4 CSS doesn't reliably support ``mask-image`` on regular widgets, so
the fade is done programmatically via Gsk.MaskNode in the snapshot pass
(same approach as FadeBottomBin).
"""

from gi.repository import Gtk, Gsk, Gdk, Graphene


class FadeEdgesBin(Gtk.Box):
    __gtype_name__ = "FadeEdgesBin"

    def __init__(self, fade_size_px=48, fade_top_px=None, fade_bottom_px=None, **kwargs):
        super().__init__(**kwargs)
        # Fade band size in CSS pixels. ``fade_top_px``/``fade_bottom_px``
        # override the symmetric ``fade_size_px`` per edge — useful when
        # the top edge needs to stay short so the first line of a list
        # doesn't dissolve, while the bottom can keep a generous fade for
        # the scroll-out effect.
        self._fade_top = max(
            0.0, float(fade_top_px if fade_top_px is not None else fade_size_px)
        )
        self._fade_bottom = max(
            0.0,
            float(fade_bottom_px if fade_bottom_px is not None else fade_size_px),
        )

    def set_fade_size(self, px):
        px = max(0.0, float(px))
        if abs(px - self._fade_top) > 0.5 or abs(px - self._fade_bottom) > 0.5:
            self._fade_top = px
            self._fade_bottom = px
            self.queue_draw()

    def do_snapshot(self, snapshot):
        w = self.get_width()
        h = self.get_height()
        if w <= 0 or h <= 0 or (self._fade_top <= 0 and self._fade_bottom <= 0):
            Gtk.Box.do_snapshot(self, snapshot)
            return

        # If the content is shorter than the combined fade bands, fall back
        # to no fade so we don't dim everything.
        if h <= (self._fade_top + self._fade_bottom) + 1:
            Gtk.Box.do_snapshot(self, snapshot)
            return

        try:
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
        except Exception:
            Gtk.Box.do_snapshot(self, snapshot)
            return

        bounds = Graphene.Rect()
        bounds.init(0, 0, w, h)
        start_pt = Graphene.Point()
        start_pt.init(0, 0)
        end_pt = Graphene.Point()
        end_pt.init(0, h)

        opaque = Gdk.RGBA()
        opaque.red = opaque.green = opaque.blue = 0.0
        opaque.alpha = 1.0
        clear = Gdk.RGBA()
        clear.red = clear.green = clear.blue = 0.0
        clear.alpha = 0.0

        top_stop = self._fade_top / h
        bot_stop = 1.0 - (self._fade_bottom / h)

        stops = []
        s = Gsk.ColorStop(); s.offset = 0.0
        s.color = clear if self._fade_top > 0 else opaque
        stops.append(s)
        s = Gsk.ColorStop(); s.offset = top_stop; s.color = opaque; stops.append(s)
        s = Gsk.ColorStop(); s.offset = bot_stop; s.color = opaque; stops.append(s)
        s = Gsk.ColorStop(); s.offset = 1.0
        s.color = clear if self._fade_bottom > 0 else opaque
        stops.append(s)

        try:
            snapshot.append_linear_gradient(bounds, start_pt, end_pt, stops)
        except Exception:
            snapshot.pop()
            Gtk.Box.do_snapshot(self, snapshot)
            return
        snapshot.pop()  # end mask source

        Gtk.Box.do_snapshot(self, snapshot)

        snapshot.pop()  # end mask block
