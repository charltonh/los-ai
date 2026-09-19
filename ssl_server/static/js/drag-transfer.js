/**
 * drag-transfer.js
 * Cross-component drag: calendar events / todo items → agenda entity nodes.
 *
 * TWO modes:
 *
 * 1. TODO mode  (initiated via mousedown on a grab handle)
 *    DragTransfer.start(item, mouseEvent)
 *    Renders a floating ghost label. Over the sidebar it shrinks to a small icon.
 *
 * 2. CALENDAR WATCH mode  (initiated when FullCalendar starts its own drag)
 *    DragTransfer.startCalendarWatch(item)
 *    No ghost – FullCalendar already shows the dragged event.
 *    We just highlight the hovered sidebar node.
 *    When FullCalendar ends the drag call:
 *    DragTransfer.checkCalendarDrop(jsEvent)  → returns true if handled by us
 *    (caller should then call info.revert() so FC doesn't place the event)
 *
 * item = { type:'calendar'|'todo', id, title, source_entity_path, source_file }
 */
(function () {
    'use strict';

    // ── Shared state ─────────────────────────────────────────────────────────
    let _currentTarget = null;   // .entity-node-content currently highlighted

    // ── TODO-drag state ──────────────────────────────────────────────────────
    let _active        = false;
    let _item          = null;
    let _ghostEl       = null;
    let _overSidebar   = false;

    // Minimum movement (px) before visual drag starts
    const THRESHOLD = 5;
    let _startX = 0, _startY = 0, _started = false;

    // ── Calendar-watch state ─────────────────────────────────────────────────
    let _calItem = null;   // item being watched (FullCalendar drag)

    // ── Public API ───────────────────────────────────────────────────────────
    window.DragTransfer = {
        /** Begin monitoring a TODO drag from a mousedown event. */
        start: _initDrag,
        isActive: () => _active || !!_calItem,

        /**
         * Called by calendar.js eventDragStart.
         * Registers the event so we can highlight sidebar nodes as it moves.
         */
        startCalendarWatch: function (item) {
            _calItem = item;
            document.addEventListener('mousemove', _onCalMove, true);
        },

        /**
         * Called by calendar.js eventDragStop (with the native jsEvent).
         * Returns true if the drop was handled by us (caller should revert FC drop).
         */
        checkCalendarDrop: function (jsEvent) {
            if (!_calItem) return false;

            const sb    = document.getElementById('agenda-sidebar');
            const overSB = sb && !sb.classList.contains('hidden') &&
                           _inEl(jsEvent.clientX, jsEvent.clientY, sb);

            let handled = false;
            if (overSB && _currentTarget) {
                const li   = _currentTarget.closest('.entity-node');
                const path = li ? li.dataset.path : null;
                if (path !== null && path !== undefined) {
                    _doMove(_calItem, path);
                    handled = true;
                }
            }
            _stopCalWatch();
            return handled;
        },

        /** Cancel the calendar watch (e.g. on eventDrop – already landed on calendar). */
        stopCalendarWatch: _stopCalWatch,
    };

    // ── Calendar-watch internals ─────────────────────────────────────────────
    function _onCalMove(e) {
        if (!_calItem) return;
        const sb    = document.getElementById('agenda-sidebar');
        const overSB = sb && !sb.classList.contains('hidden') &&
                       _inEl(e.clientX, e.clientY, sb);
        if (overSB) {
            _updateTarget(e.clientX, e.clientY);
        } else {
            _clearTarget();
        }
    }

    function _stopCalWatch() {
        _calItem = null;
        _clearTarget();
        document.removeEventListener('mousemove', _onCalMove, true);
    }

    // ── TODO-drag initiation (on mousedown) ──────────────────────────────────
    function _initDrag(item, e) {
        if (_active) return;
        _item    = item;
        _startX  = e.clientX;
        _startY  = e.clientY;
        _started = false;
        _active  = true;

        document.addEventListener('mousemove', _onMove, true);
        document.addEventListener('mouseup',   _onUp,   true);
        document.addEventListener('keydown',   _onKey,  true);
    }

    // ── TODO-drag mouse handlers ─────────────────────────────────────────────
    function _onMove(e) {
        if (!_active) return;

        if (!_started) {
            const dx = e.clientX - _startX;
            const dy = e.clientY - _startY;
            if (Math.sqrt(dx * dx + dy * dy) < THRESHOLD) return;
            _started = true;
            document.body.classList.add('dt-dragging');
            _createGhost(e.clientX, e.clientY);
        }

        _moveGhost(e.clientX, e.clientY);

        const sb    = document.getElementById('agenda-sidebar');
        const overSB = sb && !sb.classList.contains('hidden') &&
                       _inEl(e.clientX, e.clientY, sb);

        if (overSB !== _overSidebar) {
            _overSidebar = overSB;
            if (overSB) {
                // Shrink to icon so tree labels stay visible
                _ghostEl.classList.remove('dt-ghost--full');
                _ghostEl.classList.add('dt-ghost--icon');
                _ghostEl.innerHTML = '<i class="fas fa-folder-open"></i>';
            } else {
                _ghostEl.classList.remove('dt-ghost--icon');
                _ghostEl.classList.add('dt-ghost--full');
                _ghostEl.innerHTML =
                    '<i class="fas fa-grip-lines"></i> ' +
                    '<span class="dt-ghost-text">' + _esc(_item.title) + '</span>';
                _clearTarget();
            }
        }

        if (_overSidebar) _updateTarget(e.clientX, e.clientY);
    }

    function _onUp(e) {
        if (!_active) return;
        if (_started && _overSidebar && _currentTarget) {
            const li   = _currentTarget.closest('.entity-node');
            const path = li ? li.dataset.path : null;
            if (path !== null && path !== undefined) {
                _doMove(_item, path);
            }
        }
        _endDrag();
    }

    function _onKey(e) {
        if (e.key === 'Escape') _endDrag();
    }

    // ── Ghost element (TODO drag only) ───────────────────────────────────────
    function _createGhost(x, y) {
        _ghostEl = document.createElement('div');
        _ghostEl.className = 'dt-ghost dt-ghost--full';
        _ghostEl.innerHTML =
            '<i class="fas fa-grip-lines"></i> ' +
            '<span class="dt-ghost-text">' + _esc(_item.title) + '</span>';
        document.body.appendChild(_ghostEl);
        _moveGhost(x, y);
    }

    function _moveGhost(x, y) {
        if (!_ghostEl) return;
        _ghostEl.style.left = (x + 18) + 'px';
        _ghostEl.style.top  = (y - 18) + 'px';
    }

    // ── Drop-target highlighting (shared) ────────────────────────────────────
    function _updateTarget(cx, cy) {
        const nodes = document.querySelectorAll('#agenda-sidebar .entity-node-content');
        let hit = null;
        for (const n of nodes) {
            if (_inEl(cx, cy, n)) { hit = n; break; }
        }
        if (hit === _currentTarget) return;
        _clearTarget();
        if (hit) {
            hit.classList.add('dt-drop-target');
            _currentTarget = hit;
        }
    }

    function _clearTarget() {
        if (_currentTarget) {
            _currentTarget.classList.remove('dt-drop-target');
            _currentTarget = null;
        }
    }

    // ── Perform the move via backend API (shared) ────────────────────────────
    async function _doMove(item, targetEntityPath) {
        try {
            const resp = await fetch('/api/item/move', {
                method : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body   : JSON.stringify({
                    type               : item.type,
                    id                 : item.id,
                    source_entity_path : item.source_entity_path,
                    target_entity_path : targetEntityPath,
                    source_file        : item.source_file || null,
                }),
            });

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                const msg = err.error || 'HTTP ' + resp.status;
                if (resp.status === 409) {
                    // Conflict: project directory already exists at destination
                    alert('⚠️ Move conflict — ' + msg);
                    return;  // do NOT call loadEntityAgenda (item already moved in JSON but proj dir wasn't)
                }
                throw new Error(msg);
            }

            // Load the target entity (highlights it + reloads calendar/todo/cron for it)
            // without re-fetching or collapsing the tree.
            const targetName = targetEntityPath ? targetEntityPath.split('/').pop() : '';
            if (typeof window.loadEntityAgenda === 'function') {
                window.loadEntityAgenda({ path: targetEntityPath, name: targetName });
            }

            const label = targetEntityPath ? targetEntityPath.split('/').pop() : 'root';
            _toast('Moved to "' + label + '"');
        } catch (err) {
            console.error('DragTransfer move error:', err);
            _toast('Move failed: ' + err.message, true);
        }
    }

    // ── TODO-drag cleanup ────────────────────────────────────────────────────
    function _endDrag() {
        _active      = false;
        _item        = null;
        _started     = false;
        _overSidebar = false;
        _clearTarget();
        document.body.classList.remove('dt-dragging');
        if (_ghostEl) { _ghostEl.remove(); _ghostEl = null; }
        document.removeEventListener('mousemove', _onMove, true);
        document.removeEventListener('mouseup',   _onUp,   true);
        document.removeEventListener('keydown',   _onKey,  true);
    }

    // ── Utilities ────────────────────────────────────────────────────────────
    function _inEl(x, y, el) {
        const r = el.getBoundingClientRect();
        return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    }

    function _toast(msg, isErr) {
        const t = document.createElement('div');
        t.className = 'toast' + (isErr ? ' toast-error' : '');
        t.textContent = msg;
        document.body.appendChild(t);
        t.offsetHeight; // force reflow
        t.classList.add('show');
        setTimeout(() => {
            t.classList.remove('show');
            setTimeout(() => t.remove(), 300);
        }, 3000);
    }

    function _esc(s) {
        const d = document.createElement('div');
        d.textContent = s || '(untitled)';
        return d.innerHTML;
    }
})();
