"""Self-contained lyrics widget. Subscribes to the player's metadata and
progression signals, fetches lyrics in a background thread, and renders
them as a scrollable ListBox of tap-to-seek lines. Active line is centered
in the viewport; per-word data, when available, drives a karaoke-style
alpha fade.

Architecture follows Nocturne's (Jeffser/Nocturne) playing lyrics page:
- Every row always renders with ``set_markup`` (never ``set_label``), so
  Pango's layout pipeline is stable across active/inactive transitions.
- Per-row opacity comes from a ``<span fgalpha='N'>`` Pango attribute,
  which is a pure render attribute that does not affect glyph metrics.
- A per-row ``tick_callback`` lerps the alphas toward their target each
  frame and only re-renders markup when ``changed`` is true.
- Scroll target uses the row's own ``get_allocation().y`` (which lives
  in the ListBox's coordinate space — that's exactly what the
  vadjustment is offset against), wrapped in ``GLib.idle_add`` so the
  scroll happens after layout has settled.
"""

import html
import os
import threading
import time
from gi.repository import Gtk, Adw, GObject, GLib, Pango, Graphene

from ui.widgets.fade_edges_bin import FadeEdgesBin
from ui.util_classes import ScrolledWindow


# Pango's "alpha" attribute takes a 16-bit value where 65535 = fully opaque.
_PANGO_ALPHA_MAX = 65535


# Toggle with MIXTAPES_LYRICS_DEBUG=1 (or =2 for tick-level chatter).
# Logs go to stdout, prefixed with the elapsed seconds since launch and
# a [LYRICS] tag so they're easy to grep.
_DEBUG_LEVEL = int(os.environ.get("MIXTAPES_LYRICS_DEBUG") or "0")
_DEBUG_T0 = time.monotonic()


def _dlog(level, msg):
    if _DEBUG_LEVEL >= level:
        dt = time.monotonic() - _DEBUG_T0
        print(f"[LYRICS {dt:7.3f}] {msg}", flush=True)

# Opacity targets per line state.
_ALPHA_ACTIVE = 1.00
_ALPHA_INACTIVE = 0.32
_ALPHA_FUTURE_WORD = 0.32
_LERP_SPEED = 0.18  # Per-frame fade speed (0..1 fraction of remaining gap).


class LyricRow(Gtk.ListBoxRow):
    """A single lyric line. Always rendered with Pango markup so swapping
    between active and inactive doesn't re-layout the label."""

    __gtype_name__ = "MixtapesLyricRow"

    def __init__(self, line, line_idx):
        super().__init__()
        self.line_idx = line_idx
        self.start_ms = int((line.get("start") or 0.0) * 1000)
        self.text = line.get("text") or ""
        # Word-level parts (each {start, end, text}) if the source ships
        # syllable / word timing. ``None`` means line-level only.
        raw_parts = line.get("parts")
        self.parts = []
        if raw_parts:
            for p in raw_parts:
                start = p.get("start")
                end = p.get("end")
                text = p.get("text")
                if text and start is not None:
                    self.parts.append({
                        "start_ms": int(start * 1000),
                        "end_ms": int((end or start) * 1000),
                        "text": text,
                    })

        self.label = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            justify=Gtk.Justification.LEFT,
            halign=Gtk.Align.START,
            valign=Gtk.Align.CENTER,
            xalign=0.0,
        )
        self.label.add_css_class("lyrics-line-label")
        self.set_child(self.label)
        self.add_css_class("lyrics-line")
        self.set_selectable(True)
        self.set_activatable(True)
        # Don't take keyboard focus. Selecting a row would otherwise pull
        # focus onto it, and the ScrolledWindow's built-in scroll-on-focus
        # behaviour would race with our own animated scroll — visible as
        # the active line jumping back and forth.
        self.set_can_focus(False)
        self.set_focusable(False)

        # Per-word alpha state. Each word has a current alpha that lerps
        # toward a target on every frame tick. For line-only sources we
        # synthesize a single "word" covering the whole line.
        if self.parts:
            self._word_alphas = [_ALPHA_FUTURE_WORD for _ in self.parts]
            self._word_targets = [_ALPHA_FUTURE_WORD for _ in self.parts]
        else:
            self._word_alphas = [_ALPHA_INACTIVE]
            self._word_targets = [_ALPHA_INACTIVE]

        # Current playback position (ms) — drives the targets each tick.
        # Set by the parent view; -1 means "not the active line".
        self._cursor_ms = -1
        self._dirty = True
        self.add_tick_callback(self._on_tick)
        self._render_markup()

    # The parent view pushes the current cursor position into the active
    # row and (-1) into all others. The tick callback turns this into a
    # per-word alpha target.
    def set_cursor_ms(self, ms):
        if ms == self._cursor_ms:
            return
        self._cursor_ms = ms
        self._recompute_targets()

    def _recompute_targets(self):
        is_active = self._cursor_ms >= 0
        if not self.parts:
            self._word_targets[0] = _ALPHA_ACTIVE if is_active else _ALPHA_INACTIVE
            return
        for i, p in enumerate(self.parts):
            if is_active and self._cursor_ms >= p["start_ms"]:
                self._word_targets[i] = _ALPHA_ACTIVE
            else:
                self._word_targets[i] = _ALPHA_FUTURE_WORD

    def _on_tick(self, _widget, _frame_clock):
        changed = False
        for i, target in enumerate(self._word_targets):
            cur = self._word_alphas[i]
            if abs(cur - target) > 0.002:
                self._word_alphas[i] = cur + (target - cur) * _LERP_SPEED
                changed = True
        if changed or self._dirty:
            self._render_markup()
            self._dirty = False
        # Keep ticking — cheap when no alphas are in motion.
        return GLib.SOURCE_CONTINUE

    def _render_markup(self):
        if not self.parts:
            alpha = int(self._word_alphas[0] * _PANGO_ALPHA_MAX)
            escaped = html.escape(self.text)
            markup = f"<span fgalpha='{alpha}'>{escaped}</span>"
        else:
            chunks = []
            for i, p in enumerate(self.parts):
                alpha = int(self._word_alphas[i] * _PANGO_ALPHA_MAX)
                chunks.append(
                    f"<span fgalpha='{alpha}'>{html.escape(p['text'])}</span>"
                )
            markup = " ".join(chunks)
        self.label.set_markup(markup)


class LyricsView(Gtk.Box):
    """A column of lyrics for the currently-playing track."""

    _next_dbg_id = 0

    def __init__(self, player, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self.player = player
        self.add_css_class("lyrics-view")
        self.set_hexpand(True)
        self.set_vexpand(True)
        LyricsView._next_dbg_id += 1
        self._dbg_id = LyricsView._next_dbg_id
        if _DEBUG_LEVEL >= 1:
            _dlog(1, f"LyricsView #{self._dbg_id} init "
                     f"(debug level {_DEBUG_LEVEL})")
        # Re-activate the right line whenever this view becomes mapped, so
        # the queue/lyrics tab user just switched to picks up the current
        # playback position. While unmapped we drop scroll work to keep
        # the hidden duplicate (mobile expanded player vs desktop cover
        # view) from racing the visible one's autoscroll.
        self.connect("map", self._on_map)

        # Async fetch generation token — invalidates stale in-flight fetches.
        self._fetch_gen = 0
        self._current_video_id = None
        self._lines = []
        self._synced = False
        self._active_idx = -1
        self._last_pos = 0.0

        # Suspend autoscroll for a short window after the user manually
        # scrolls so we don't fight them.
        import time as _time
        self._time = _time
        self._user_scrolled_at = 0.0
        self._user_scroll_pause = 4.0
        self._suppress_select_signal = False
        self._scroll_anim_source = 0
        # The line index our most-recent scroll request targets. If it
        # changes before a deferred do_scroll fires, that scroll is
        # superseded and bails out.
        self._scroll_target_idx = -1

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.append(self.stack)

        # --- Loading page ---
        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12
        )
        loading_box.set_valign(Gtk.Align.CENTER)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.set_vexpand(True)
        spinner = Adw.Spinner()
        spinner.set_size_request(36, 36)
        loading_box.append(spinner)
        self.stack.add_named(loading_box, "loading")

        # --- Empty / no-lyrics page ---
        self.status_page = Adw.StatusPage()
        # ``emblem-music-symbolic`` doesn't ship with Adwaita; use the same
        # icon as the lyrics toggle so "no lyrics" reads as a dimmed
        # version of the affordance the user just clicked.
        self.status_page.set_icon_name("format-justify-fill-symbolic")
        self.status_page.set_title("No lyrics")
        self.status_page.set_description("No lyrics found for this track.")
        self.status_page.set_vexpand(True)
        self.stack.add_named(self.status_page, "empty")

        # --- Lyrics page ---
        self.scroller = ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        self.scroller.add_css_class("lyrics-scroller")

        # Gtk.ListBox: each row is a LyricRow. Selecting a row drives both
        # the highlight (via :selected style) and the autoscroll.
        self.lrc_list = Gtk.ListBox()
        self.lrc_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.lrc_list.add_css_class("lyrics-list")
        # Small top margin (just enough to keep the first line out of the
        # short top-edge fade band) and a generous bottom one so the last
        # few lines can still scroll into the viewport center.
        self.lrc_list.set_margin_top(32)
        self.lrc_list.set_margin_bottom(400)
        self.lrc_list.set_margin_start(16)
        self.lrc_list.set_margin_end(16)
        self.lrc_list.connect("row-selected", self._on_row_selected)
        self.lrc_list.connect("row-activated", self._on_row_activated)

        # Wider clamp than the typical Adw.Clamp default so the lyrics
        # column actually uses the sidebar room when it's available.
        # Tightening kicks in earlier so the column compresses gradually
        # rather than snapping at the max.
        clamp = Adw.Clamp()
        clamp.set_maximum_size(820)
        clamp.set_tightening_threshold(640)
        clamp.set_child(self.lrc_list)
        self.scroller.set_child(clamp)

        # Asymmetric fade: barely-there at the top so the first line of
        # lyrics stays fully readable, generous at the bottom for the
        # scroll-out effect against the player bar / chrome below.
        fade = FadeEdgesBin(fade_top_px=20, fade_bottom_px=80)
        fade.set_orientation(Gtk.Orientation.VERTICAL)
        fade.set_hexpand(True)
        fade.set_vexpand(True)
        fade.append(self.scroller)

        # Floating source-picker button in the top-right corner. Opens
        # a popover listing every provider's result for the current
        # track so the user can switch sources when the chain-picked
        # default has bad timing or a wrong-language version.
        self._lyrics_page_overlay = Gtk.Overlay()
        self._lyrics_page_overlay.set_child(fade)

        # Small floating menu button. Just an icon — the current source
        # is shown as a checkmark inside the popover. The button needs to
        # work over busy backgrounds (album art on desktop, dark window
        # bg on mobile), so it carries a soft translucent surface.
        self._source_picker_btn = Gtk.MenuButton()
        self._source_picker_btn.set_icon_name("view-more-symbolic")
        self._source_picker_btn.set_tooltip_text("Choose lyrics source")
        self._source_picker_btn.add_css_class("lyrics-source-btn")
        # Drop the default chunky button chrome that draws inside the
        # MenuButton — we paint our own soft disc via the CSS class.
        self._source_picker_btn.set_has_frame(False)
        self._source_picker_btn.set_halign(Gtk.Align.END)
        self._source_picker_btn.set_valign(Gtk.Align.START)
        self._source_picker_btn.set_margin_top(10)
        self._source_picker_btn.set_margin_end(10)
        self._source_picker_btn.set_visible(False)
        # No internal label — kept for compatibility with helpers that
        # update the visible button copy on source change.
        self._source_picker_label = None

        self._source_picker_popover = self._build_source_picker_popover()
        self._source_picker_btn.set_popover(self._source_picker_popover)
        self._source_picker_btn.connect(
            "notify::active", self._on_source_picker_toggled,
        )
        self._lyrics_page_overlay.add_overlay(self._source_picker_btn)

        self.stack.add_named(self._lyrics_page_overlay, "lyrics")

        # Detect manual scrolling so autoscroll pauses while the user is
        # interacting with the view. Only the scroll controller — the
        # earlier GestureDrag fired false positives on incidental
        # mouse-button movement inside the lyrics area, suspending the
        # autoscroll for several seconds at random.
        scroll_ctl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll_ctl.connect("scroll", self._on_user_scroll)
        self.scroller.add_controller(scroll_ctl)

        self.stack.set_visible_child_name("empty")

        self.player.connect("metadata-changed", self._on_metadata_changed)
        self.player.connect("progression", self._on_progression)
        self.player.connect("state-changed", self._on_state_changed)

        if self.player.current_video_id:
            self._refresh_for_current_track()

    def _log(self, level, msg):
        _dlog(level, f"#{self._dbg_id} {msg}")

    # ── Source picker ─────────────────────────────────────────────────────

    def _build_source_picker_popover(self):
        pop = Gtk.Popover()
        pop.set_position(Gtk.PositionType.BOTTOM)
        pop.set_size_request(260, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        # No margins on the container — the popover already provides its
        # own chrome.

        header = Gtk.Label(label="Lyrics source")
        header.add_css_class("heading")
        header.set_halign(Gtk.Align.START)
        header.set_margin_start(4)
        header.set_margin_top(4)
        outer.append(header)

        # Mirrors the add-to-playlist popover: ``navigation-sidebar`` for
        # the row-hover effect without the ``boxed-list`` border/shadow
        # that was making each entry look like its own card.
        self._source_picker_list = Gtk.ListBox()
        self._source_picker_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._source_picker_list.add_css_class("navigation-sidebar")
        self._source_picker_list.add_css_class("lyrics-source-list")
        self._source_picker_list.connect("row-activated", self._on_source_row_activated)
        outer.append(self._source_picker_list)

        self._source_picker_spinner_row = Gtk.ListBoxRow()
        self._source_picker_spinner_row.set_selectable(False)
        self._source_picker_spinner_row.set_activatable(False)
        sb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sb.set_margin_top(6); sb.set_margin_bottom(6)
        sb.set_margin_start(8); sb.set_margin_end(8)
        sb.set_halign(Gtk.Align.CENTER)
        spinner = Adw.Spinner()
        spinner.set_size_request(16, 16)
        sb.append(spinner)
        lab = Gtk.Label(label="Looking for other sources…")
        lab.add_css_class("dim-label")
        sb.append(lab)
        self._source_picker_spinner_row.set_child(sb)

        pop.set_child(outer)
        return pop

    def _on_source_picker_toggled(self, btn, *_):
        if not btn.get_active():
            return
        # Repopulate every time the picker opens so the rows reflect the
        # current cache state (in case the user changed tracks).
        self._refresh_source_picker_rows(include_spinner=True)
        if not self._current_video_id:
            return
        # Fire any uncached provider in the background; the callback adds
        # rows as results arrive.
        title, artist, duration = self._track_metadata(self._current_video_id)

        def _on_alt(source, result):
            GLib.idle_add(self._on_alt_result, source, result)

        try:
            self.player.client.fetch_lyrics_alternatives_async(
                self._current_video_id, title, artist, duration, _on_alt,
            )
        except Exception as e:
            self._log(1, f"alternatives fetch failed to start: {e}")

    def _refresh_source_picker_rows(self, include_spinner):
        # Clear out existing rows.
        child = self._source_picker_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._source_picker_list.remove(child)
            child = nxt

        if not self._current_video_id:
            return

        client = self.player.client
        alts = client.get_lyrics_alternatives(self._current_video_id)
        preferred = client.get_preferred_lyrics_source(self._current_video_id)
        active_source = self._active_source_name()

        for source, result in alts:
            self._source_picker_list.append(
                self._build_source_row(
                    source, result,
                    is_active=(source == active_source),
                    is_preferred=(source == preferred),
                )
            )

        if include_spinner:
            self._source_picker_list.append(self._source_picker_spinner_row)

    def _build_source_row(self, source, result, is_active, is_preferred):
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        # Tag the row with its source name so the activation handler
        # knows what to switch to.
        row._lyrics_source = source

        # No inner margins on the hbox — the listbox's `.lyrics-source-
        # list` CSS rule provides the only padding so it stays in sync
        # with the rest of the popover layout.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text.set_hexpand(True)

        title = Gtk.Label(label=source)
        title.set_halign(Gtk.Align.START)
        title.add_css_class("heading")
        text.append(title)

        n = len(result.get("lines") or [])
        has_word = any(l.get("parts") for l in result.get("lines") or [])
        synced = bool(result.get("synced"))
        if has_word:
            kind = "word-level"
        elif synced:
            kind = "synced"
        else:
            kind = "plain"
        suffix = " · pinned" if is_preferred else ""
        sub = Gtk.Label(label=f"{n} lines · {kind}{suffix}")
        sub.set_halign(Gtk.Align.START)
        sub.add_css_class("caption")
        sub.set_opacity(0.7)
        text.append(sub)

        box.append(text)

        if is_active:
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_valign(Gtk.Align.CENTER)
            box.append(check)

        row.set_child(box)
        return row

    def _on_alt_result(self, source, result):
        # A provider finished. Refresh the popover rows so the new
        # source shows up (or the spinner can be hidden when all done).
        if not self._source_picker_btn.get_active():
            return False
        # Determine whether all known providers have reported.
        client = self.player.client
        cached_sources = {s for s, _ in client.get_lyrics_alternatives(
            self._current_video_id or "")}
        all_done = all(
            name in cached_sources or name == source
            for name, _attr, _vid in client._LYRIC_PROVIDERS
        )
        self._refresh_source_picker_rows(include_spinner=not all_done)
        return False

    def _on_source_row_activated(self, listbox, row):
        source = getattr(row, "_lyrics_source", None)
        if not source or not self._current_video_id:
            return
        client = self.player.client
        # Pin the choice so future loads of this track return the same
        # source.
        client.set_preferred_lyrics_source(self._current_video_id, source)
        # Switch the currently-displayed lyrics to that source's data.
        alts = dict(client.get_lyrics_alternatives(self._current_video_id))
        data = alts.get(source)
        if data:
            self._switch_to_source(source, data)
        # Close the popover and update its checkmark for next open.
        self._source_picker_btn.set_active(False)

    def _switch_to_source(self, source, data):
        """Re-render the lyrics view with a different provider's data
        for the current track."""
        self._log(1, f"switching to source={source}")
        self._lines = data.get("lines") or []
        self._synced = bool(data.get("synced"))
        self._current_source = source
        self._active_idx = -1
        self._build_rows()
        self.stack.set_visible_child_name("lyrics")
        self._update_source_picker_label()
        if self._synced and getattr(self.player, "duration", 0) > 0:
            idx = self._index_for_position(self._last_pos)
            self._activate_row(idx, cursor_ms=int(self._last_pos * 1000))

    def _active_source_name(self):
        return getattr(self, "_current_source", None)

    def _update_source_picker_label(self):
        # The picker is icon-only now — the active source is shown via
        # the checkmark inside the popover. Kept as a no-op so callers
        # don't need to check whether a label exists.
        name = self._active_source_name()
        if name:
            self._source_picker_btn.set_tooltip_text(f"Lyrics source: {name}")

    # ── Public API ─────────────────────────────────────────────────────────

    def refresh(self):
        if self._current_video_id:
            cache = getattr(self.player.client, "_lyrics_cache", None)
            if cache:
                cache.pop(self._current_video_id, None)
            self._refresh_for_current_track(force=True)

    # ── Player signal handlers ─────────────────────────────────────────────

    def _on_metadata_changed(self, player, title, artist, thumb, video_id, like):
        if video_id == self._current_video_id and self._lines:
            return
        self._log(1, f"metadata-changed: video_id={video_id!r} title={title!r}")
        self._current_video_id = video_id or None
        self._refresh_for_current_track()

    def _on_progression(self, player, pos, dur):
        self._last_pos = pos
        if not self._synced or not self._lines:
            return
        # Two LyricsView instances exist (mobile expanded player +
        # desktop cover view). Only the mapped one should drive scrolls
        # so they don't race each other and double up signal handlers.
        if not self.get_mapped():
            # Still record the active idx so the next progression after
            # we become visible doesn't re-trigger a stale activation.
            new_idx = self._index_for_position(pos)
            self._active_idx = new_idx
            return
        idx = self._index_for_position(pos)
        ms = int(pos * 1000)
        if idx != self._active_idx:
            self._log(1, f"progression pos={pos:.2f}s -> idx changed "
                         f"{self._active_idx} -> {idx}")
            self._activate_row(idx, cursor_ms=ms)
        elif 0 <= idx:
            row = self._row_at(idx)
            if row is not None:
                row.set_cursor_ms(ms)
            self._log(2, f"progression pos={pos:.2f}s same idx={idx}")

    def _on_map(self, *_):
        # Switching into this view: jump straight to the correct line
        # without animation so the user lands on the right spot.
        self._log(1, "view mapped")
        if self._synced and self._lines:
            pos = self._last_pos
            idx = self._index_for_position(pos)
            # Force a re-activation even if idx didn't change while we
            # were hidden — the row may not have been scrolled to.
            self._active_idx = -1
            self._activate_row(idx, cursor_ms=int(pos * 1000))

    def _on_state_changed(self, player, state):
        if state == "stopped" and not self.player.current_video_id:
            self._current_video_id = None
            self._lines = []
            self._render_status("empty", title="Not playing")

    # ── Fetch pipeline ─────────────────────────────────────────────────────

    def _refresh_for_current_track(self, force=False):
        vid = self._current_video_id
        if not vid:
            self._lines = []
            self._active_idx = -1
            self._clear_rows()
            self._render_status("empty", title="Not playing",
                                description="Play a song to see lyrics.")
            return

        title, artist, duration = self._track_metadata(vid)

        self._fetch_gen += 1
        gen = self._fetch_gen
        # Wipe ALL state before kicking off the fetch so a late
        # progression event can't index into stale line data.
        self._lines = []
        self._synced = False
        self._active_idx = -1
        self._current_source = None
        self._source_picker_btn.set_visible(False)
        self._clear_rows()
        self.stack.set_visible_child_name("loading")

        def _worker():
            try:
                data = self.player.client.get_lyrics(
                    vid, title=title, artist=artist, duration=duration,
                )
            except Exception as e:
                print(f"[LYRICS] fetch error: {e}")
                data = None
            GLib.idle_add(self._apply_fetch_result, gen, data)

        threading.Thread(target=_worker, daemon=True).start()

    def _track_metadata(self, video_id):
        title, artist, duration = None, None, None
        try:
            idx = self.player.current_queue_index
            if 0 <= idx < len(self.player.queue):
                track = self.player.queue[idx]
                if track.get("videoId") == video_id:
                    title = track.get("title")
                    artists = track.get("artists") or []
                    if artists and isinstance(artists, list):
                        names = [a.get("name") for a in artists if isinstance(a, dict)]
                        names = [n for n in names if n]
                        if names:
                            artist = names[0]
                    if not artist:
                        artist = track.get("artist")
                    from player.player import _parse_track_duration
                    duration = _parse_track_duration(track) or None
        except Exception:
            pass
        if not duration and getattr(self.player, "duration", 0) > 0:
            duration = int(self.player.duration)
        return title, artist, duration

    def _apply_fetch_result(self, gen, data):
        if gen != self._fetch_gen:
            self._log(1, f"fetch result stale (gen={gen} != {self._fetch_gen}), dropping")
            return False

        if not data or not data.get("lines"):
            self._log(1, "fetch result: no lyrics")
            self._lines = []
            self._synced = False
            self._render_status(
                "empty", title="No lyrics",
                description="We couldn't find lyrics for this track.",
            )
            return False

        self._lines = data["lines"]
        self._synced = bool(data.get("synced"))
        self._current_source = data.get("source")
        word_lines = sum(1 for l in self._lines if l.get("parts"))
        self._log(1, f"fetch result: source={data.get('source')} "
                 f"synced={self._synced} lines={len(self._lines)} "
                 f"word-level={word_lines}")
        self._build_rows()
        self.stack.set_visible_child_name("lyrics")
        # Reveal the source-picker button now that we have at least one
        # provider's data in the cache.
        self._source_picker_btn.set_visible(True)
        self._update_source_picker_label()

        if self._synced and getattr(self.player, "duration", 0) > 0:
            idx = self._index_for_position(self._last_pos)
            self._log(1, f"initial activate at idx={idx} pos={self._last_pos:.2f}s")
            self._activate_row(idx, cursor_ms=int(self._last_pos * 1000))
        else:
            adj = self.scroller.get_vadjustment()
            if adj:
                adj.set_value(adj.get_lower())
        return False

    # ── Row management ────────────────────────────────────────────────────

    def _clear_rows(self):
        # GTK 4.12+ has remove_all; earlier versions need a loop. Use the
        # iterative removal for compatibility.
        child = self.lrc_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lrc_list.remove(child)
            child = nxt

    def _build_rows(self):
        self._clear_rows()
        for i, line in enumerate(self._lines):
            row = LyricRow(line, i)
            # Plain (unsynced) sources have no cursor to scrub against, so
            # the active-line dim/bright contrast just communicates "this
            # is offline text" if every line stayed at INACTIVE alpha.
            # Activate every row so the whole block reads at full
            # brightness — the visual cue for "no sync info".
            if not self._synced:
                row.set_cursor_ms(0)
            self.lrc_list.append(row)
        self._log(1, f"built {len(self._lines)} rows, "
                 f"lrc_list margins t={self.lrc_list.get_margin_top()} "
                 f"b={self.lrc_list.get_margin_bottom()}")

    def _row_at(self, idx):
        return self.lrc_list.get_row_at_index(idx)

    # ── Activation + autoscroll ───────────────────────────────────────────

    def _index_for_position(self, pos):
        if not self._lines:
            return -1
        active = -1
        for i, line in enumerate(self._lines):
            start = line.get("start")
            if start is None:
                continue
            if start <= pos:
                active = i
            else:
                break
        return active

    def _activate_row(self, idx, cursor_ms):
        # Clear the previously-active row's cursor so its words fade back.
        if 0 <= self._active_idx:
            prev = self._row_at(self._active_idx)
            if prev is not None:
                prev.set_cursor_ms(-1)
        self._active_idx = idx
        if idx < 0:
            self._log(1, "activate idx=-1 (no active line yet)")
            return
        row = self._row_at(idx)
        if row is None:
            self._log(1, f"activate idx={idx} but row_at returned None")
            return
        row.set_cursor_ms(cursor_ms)
        self._log(1, f"activate idx={idx} cursor_ms={cursor_ms} "
                 f"-> calling select_row")
        # Selecting the row drives both visual state (:selected style) and
        # autoscroll (via _on_row_selected). Suppress the click-to-seek
        # path since this selection isn't user-initiated.
        self._suppress_select_signal = True
        self.lrc_list.select_row(row)
        self._suppress_select_signal = False

    def _on_row_selected(self, listbox, row):
        if row is None:
            self._log(1, "row-selected fired with row=None")
            return
        self._log(1, f"row-selected fired for idx={row.line_idx} "
                 f"(suppress_flag={self._suppress_select_signal})")
        # Autoscroll, regardless of who selected the row.
        self._scroll_to_row(row)

    def _on_row_activated(self, listbox, row):
        # User clicked a row → seek the player to that line's timestamp.
        if self._suppress_select_signal or row is None:
            return
        if not isinstance(row, LyricRow):
            return
        if not self._synced:
            return
        start = (row.start_ms or 0) / 1000.0
        if self.player.duration > 0:
            self.player.seek(start)

    def _scroll_to_row(self, row):
        # Respect a recent manual scroll for a short grace window.
        since = self._time.monotonic() - self._user_scrolled_at
        if since < self._user_scroll_pause:
            self._log(1, f"scroll_to_row idx={row.line_idx} BLOCKED "
                     f"(user scrolled {since:.2f}s ago)")
            return
        # Capture the row's "generation" — if the active row has moved on
        # by the time the deferred scroll fires, abort. Multiple deferred
        # scrolls queueing in idle order was causing the active line to
        # snap back and forth as out-of-date callbacks executed.
        target_idx = row.line_idx
        self._scroll_target_idx = target_idx
        self._log(1, f"scroll_to_row idx={target_idx} scheduled")

        def do_scroll(retries=8):
            if self._scroll_target_idx != target_idx:
                self._log(1, f"do_scroll idx={target_idx} SUPERSEDED "
                         f"(now targeting {self._scroll_target_idx})")
                return False  # Superseded by a newer activation.
            adj = self.scroller.get_vadjustment()
            content = self.scroller.get_child()
            if adj is None or content is None:
                self._log(1, f"do_scroll idx={target_idx} no adj/content")
                return False
            alloc = row.get_allocation()
            if alloc.height <= 0:
                self._log(2, f"do_scroll idx={target_idx} row not laid out "
                         f"(retries={retries})")
                if retries > 0:
                    GLib.timeout_add(33, do_scroll, retries - 1)
                return False
            # Use row.get_allocation().y directly. Empirically,
            # compute_point(row, scroller.get_child()) in this widget
            # hierarchy factors in the scroll transform — its result
            # equals `alloc.y - adj.value + padding`. Subtracting vh/2
            # from a scroll-relative y produces a target that drifts
            # by the current adj.value each activation, so the active
            # line creeps toward the bottom of the viewport. alloc.y is
            # invariant of scroll: it's the row's true y inside the
            # ListBox, which (with the ListBox at the top of the Clamp,
            # margin_top=0) equals its y in the scrollable content.
            viewport_h = self.scroller.get_height()
            if viewport_h <= 0:
                self._log(1, f"do_scroll idx={target_idx} viewport not "
                         f"realized (vh={viewport_h}) — likely a hidden "
                         f"LyricsView; skipping")
                return False
            target = alloc.y - (viewport_h / 2) + (alloc.height / 2)
            raw_target = target
            target = max(adj.get_lower(),
                         min(target, adj.get_upper() - adj.get_page_size()))
            self._log(1,
                f"do_scroll idx={target_idx} "
                f"row.alloc=(y={alloc.y},h={alloc.height}) "
                f"vh={viewport_h} "
                f"raw_target={raw_target:.1f} clamped={target:.1f} "
                f"adj=(val={adj.get_value():.1f},lo={adj.get_lower():.1f},"
                f"up={adj.get_upper():.1f},page={adj.get_page_size():.1f})")
            self._animate_to(adj, target)
            return False

        GLib.idle_add(do_scroll)

    def _animate_to(self, adj, target, duration_ms=320):
        """Smoothly scroll the adjustment from its current value to
        ``target`` over ``duration_ms`` with an ease-out curve. Any
        in-flight animation is cancelled first so consecutive calls
        seamlessly retarget."""
        if self._scroll_anim_source:
            self._log(1, f"animate_to: cancelling in-flight animation")
            GLib.source_remove(self._scroll_anim_source)
            self._scroll_anim_source = 0
        start_value = adj.get_value()
        if abs(start_value - target) < 1.0:
            self._log(1, f"animate_to: no-op (start={start_value:.1f} ≈ "
                     f"target={target:.1f})")
            adj.set_value(target)
            return
        self._log(1, f"animate_to: start={start_value:.1f} -> target={target:.1f} "
                 f"delta={target - start_value:+.1f}")
        start_time = GLib.get_monotonic_time()

        def _tick():
            elapsed = (GLib.get_monotonic_time() - start_time) / 1000.0
            t = min(1.0, elapsed / max(1, duration_ms))
            eased = 1.0 - (1.0 - t) ** 3
            adj.set_value(start_value + (target - start_value) * eased)
            if t >= 1.0:
                self._scroll_anim_source = 0
                self._log(2, f"animate_to: done at {adj.get_value():.1f}")
                return False
            return True

        self._scroll_anim_source = GLib.timeout_add(16, _tick)

    def _render_status(self, name, title=None, description=None):
        if title is not None:
            self.status_page.set_title(title)
        if description is not None:
            self.status_page.set_description(description)
        self.stack.set_visible_child_name(name)

    def _on_user_scroll(self, *_):
        self._user_scrolled_at = self._time.monotonic()
        self._log(1, "user-scroll detected -> autoscroll paused")
