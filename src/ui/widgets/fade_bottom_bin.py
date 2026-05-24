"""FadeBottomBin: a Gtk.Box subclass that fades its child content to
alpha 0 at the bottom via a Gsk.MaskNode applied in snapshot.

Used by the artist banner in blur mode so the photo blends smoothly
into the blurred cover background behind it. GTK4 CSS doesn't reliably
support `mask-image` on regular widgets, so the fade has to be done
programmatically in the snapshot pass.
"""

from gi.repository import Gtk, Gsk, Gdk, Graphene


class FadeBottomBin(Gtk.Box):
    __gtype_name__ = "FadeBottomBin"

    def __init__(self, fade_start=0.55, **kwargs):
        super().__init__(**kwargs)
        # When True, masks the bottom of the widget's content to alpha 0
        # via a vertical gradient. When False, snapshots children
        # unchanged — important so the widget is invisible (no fade) in
        # non-blur mode where the regular scrim does the work.
        self._fade_active = False
        # Fraction (0..1) of the height where the fade starts. Above
        # this point alpha is fully opaque; below, it ramps to 0 at the
        # very bottom. 0.55 → top ~55% is solid, bottom 45% fades.
        self._fade_start = max(0.0, min(1.0, float(fade_start)))

    def set_fade_active(self, active):
        active = bool(active)
        print(f"[FADE] set_fade_active({active}) (was {self._fade_active})")
        if active != self._fade_active:
            self._fade_active = active
            self.queue_draw()

    def get_fade_active(self):
        return self._fade_active

    def do_snapshot(self, snapshot):
        if not self._fade_active:
            Gtk.Box.do_snapshot(self, snapshot)
            return

        w = self.get_width()
        h = self.get_height()
        if w <= 0 or h <= 0:
            Gtk.Box.do_snapshot(self, snapshot)
            return

        if not getattr(self, "_fade_first_paint_logged", False):
            self._fade_first_paint_logged = True
            print(f"[FADE] first masked snapshot w={w} h={h}")

        # push_mask defines a region whose first child is the mask
        # source and second child is the content. We pop twice: once
        # after the source, once after the content.
        try:
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
        except Exception as e:
            print(f"[FADE] push_mask failed: {type(e).__name__}: {e}")
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

        stop_top = Gsk.ColorStop()
        stop_top.offset = 0.0
        stop_top.color = opaque
        stop_hold = Gsk.ColorStop()
        stop_hold.offset = self._fade_start
        stop_hold.color = opaque
        stop_bottom = Gsk.ColorStop()
        stop_bottom.offset = 1.0
        stop_bottom.color = clear

        try:
            snapshot.append_linear_gradient(
                bounds, start_pt, end_pt, [stop_top, stop_hold, stop_bottom]
            )
        except Exception as e:
            print(f"[FADE] append_linear_gradient failed: {type(e).__name__}: {e}")
            snapshot.pop()  # bail out of mask cleanly
            Gtk.Box.do_snapshot(self, snapshot)
            return
        snapshot.pop()  # end mask source

        Gtk.Box.do_snapshot(self, snapshot)

        snapshot.pop()  # end mask block
