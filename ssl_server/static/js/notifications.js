/**
 * notifications.js — Live agent-callback notification bell
 *
 * Connects to /api/notifications/events (SSE) to receive real-time badge
 * updates when an OpenClaw async job completes and posts back via the
 * /api/agent/webhook endpoint.
 *
 * Bell click  → opens/closes the panel and fetches the full list
 * Mark-all    → marks every notification read and resets the badge
 * Item click  → marks that notification read, reveals the canvas window and
 *               dispatches a canvas-task-selected event so the canvas opens
 *               and flash-highlights the block this callback created
 *               (matched by message id, falling back to job id)
 */

(function () {
    'use strict';

    // ── DOM refs ──────────────────────────────────────────────────────────
    const btn       = document.getElementById('notif-btn');
    const badge     = document.getElementById('notif-badge');
    const panel     = document.getElementById('notif-panel');
    const list      = document.getElementById('notif-list');
    const markAll   = document.getElementById('notif-mark-all');

    if (!btn || !badge || !panel || !list) return;   // guard for non-dashboard pages

    // ── State ─────────────────────────────────────────────────────────────
    let unreadCount = 0;
    let panelOpen   = false;
    let es          = null;    // EventSource

    // ── Badge helpers ─────────────────────────────────────────────────────
    function setCount(n) {
        unreadCount = Math.max(0, n);
        if (unreadCount > 0) {
            badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
            badge.style.display = 'inline-flex';
            btn.classList.add('notif-has-unread');
        } else {
            badge.style.display = 'none';
            btn.classList.remove('notif-has-unread');
        }
    }

    // ── Render a single notification row ──────────────────────────────────
    function makeRow(n) {
        const row = document.createElement('div');
        row.className = 'notif-item' + (n.read ? ' notif-read' : ' notif-unread');
        row.dataset.id       = n.id;
        row.dataset.taskId   = n.task_id   || '';
        row.dataset.entityPath = n.entity_path || '';

        const ts = n.timestamp ? new Date(n.timestamp).toLocaleString(undefined, {
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit'
        }) : '';

        row.innerHTML = `
            <div class="notif-item-top">
                <span class="notif-item-label">
                    <i class="fas fa-robot"></i> ${escHtml(n.label || 'agent')}
                </span>
                <span class="notif-item-time">${ts}</span>
            </div>
            <div class="notif-item-snippet">${escHtml(n.snippet || '')}</div>
        `;

        row.addEventListener('click', () => onNotifClick(n, row));
        return row;
    }

    function escHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // ── Render the full list ──────────────────────────────────────────────
    function renderList(notifications) {
        list.innerHTML = '';
        if (!notifications || notifications.length === 0) {
            list.innerHTML = '<div class="notif-empty"><i class="fas fa-check-circle"></i> No notifications</div>';
            return;
        }
        notifications.forEach(n => list.appendChild(makeRow(n)));
    }

    // ── Click on a single notification ───────────────────────────────────
    async function onNotifClick(n, row) {
        // Mark it read locally
        row.classList.remove('notif-unread');
        row.classList.add('notif-read');

        // Mark read on the server (fire-and-forget)
        fetch(`/api/notifications/${encodeURIComponent(n.id)}/read`, { method: 'POST' })
            .then(r => r.json())
            .then(d => { if (d.unread !== undefined) setCount(d.unread); })
            .catch(() => {});

        // 1. Switch to the correct agenda entity if needed
        const targetEntity = n.entity_path || '';
        const currentEntity = (typeof window.getCurrentEntityPath === 'function')
            ? window.getCurrentEntityPath() : '';

        if (targetEntity !== currentEntity) {
            // Try to find the node in the agenda tree and switch to it
            const treeLi = document.querySelector(`#agenda-tree li[data-path="${targetEntity}"]`);
            if (treeLi && window.loadEntityAgenda) {
                window.loadEntityAgenda({ path: targetEntity, name: treeLi.querySelector('.entity-node-content span')?.textContent || '' });
            } else if (window.loadEntityAgenda) {
                // Fallback: dispatch entity-selected without visual tree selection
                window.loadEntityAgenda({ path: targetEntity, name: '' });
            }
        }

        // 2. Switch to the appropriate tab and select/highlight the task
        const taskType = n.task_type || 'project';
        const tabMap = { 'calendar': 'calendar-tab', 'todo': 'todo-tab', 'crontab': 'crontab-tab', 'project': null };
        const targetTab = tabMap[taskType];

        if (targetTab) {
            // Switch tab
            const tabBtn = document.querySelector(`.tab-button[data-tab="${targetTab}"]`);
            if (tabBtn) tabBtn.click();

            // Wait for data to load then highlight/select the task
            setTimeout(() => {
                if (taskType === 'todo' && typeof window.highlightTodoById === 'function') {
                    window.highlightTodoById(n.task_id);
                    const todo = window.todos ? window.todos.find(t => t.id === n.task_id) : null;
                    if (todo && typeof loadTodoContent === 'function') {
                        loadTodoContent(n.task_id, targetEntity);
                    }
                } else if (taskType === 'calendar' && typeof window.highlightCalendarEventById === 'function') {
                    window.highlightCalendarEventById(n.task_id);
                } else if (taskType === 'crontab' && typeof window.highlightCrontabById === 'function') {
                    window.highlightCrontabById(n.task_id);
                    if (typeof loadCrontabContent === 'function') {
                        loadCrontabContent(n.task_id, '');
                    }
                }
            }, 300);
        }

        // 2b. Even for project-type notifications, try to find the task in
        //     calendar/todo/cron views and highlight it there too.
        if (taskType === 'project' && n.task_id) {
            // Try each view in turn — the task may exist in any of them
            const tryHighlight = () => {
                // Try todo list first
                if (typeof window.highlightTodoById === 'function') {
                    const todo = window.todos ? window.todos.find(t => t.id === n.task_id) : null;
                    if (todo) {
                        const tabBtn = document.querySelector('.tab-button[data-tab="todo-tab"]');
                        if (tabBtn) tabBtn.click();
                        setTimeout(() => {
                            window.highlightTodoById(n.task_id);
                            if (typeof loadTodoContent === 'function') {
                                loadTodoContent(n.task_id, targetEntity);
                            }
                        }, 300);
                        return; // Found in todo — done
                    }
                }
                // Try calendar
                if (typeof window.highlightCalendarEventById === 'function') {
                    const tabBtn = document.querySelector('.tab-button[data-tab="calendar-tab"]');
                    if (tabBtn) tabBtn.click();
                    setTimeout(() => {
                        window.highlightCalendarEventById(n.task_id);
                    }, 300);
                    return; // May or may not exist — we tried
                }
                // Try crontab
                if (typeof window.highlightCrontabById === 'function') {
                    const entry = window.crontabEntries ? window.crontabEntries.find(e => e.id === n.task_id) : null;
                    if (entry) {
                        const tabBtn = document.querySelector('.tab-button[data-tab="crontab-tab"]');
                        if (tabBtn) tabBtn.click();
                        setTimeout(() => {
                            window.highlightCrontabById(n.task_id);
                            if (typeof loadCrontabContent === 'function') {
                                loadCrontabContent(n.task_id, '');
                            }
                        }, 300);
                    }
                }
            };
            // Give the entity switch a moment to load data first
            setTimeout(tryHighlight, 400);
        }

        // 3. If this is an Entity dialogue notification, switch AI context to Entity mode
        if (n.task_id === 'entity' && window.setAiContextMode) {
            window.setAiContextMode('entity', '');
        }

        // 4. Navigate to the project in the canvas and highlight the block
        if (n.task_id) {
            // The canvas window is display:none below 3000px unless the
            // canvas-focus class is on <body>, so make sure it is visible
            // before asking the canvas to show this task.
            revealCanvasWindow();
            document.dispatchEvent(new CustomEvent('canvas-task-selected', {
                detail: {
                    taskId:     n.task_id,
                    entityPath: targetEntity,
                    label:      n.label || 'agent',
                    messageId:  n.message_id || '',
                    jobId:      n.job_id || '',
                    highlight:  true,
                }
            }));
        }

        // Close panel
        closePanel();
    }

    // ── Fetch full list from server ───────────────────────────────────────
    async function fetchAndRender() {
        try {
            const resp = await fetch('/api/notifications');
            if (!resp.ok) return;
            const data = await resp.json();
            renderList(data.notifications || []);
            setCount(data.unread || 0);
        } catch (e) {
            list.innerHTML = '<div class="notif-empty">Error loading notifications</div>';
        }
    }

    // ── Reveal the canvas window ──────────────────────────────────────────
    // Below 3000px #additional-window is display:none unless the body carries
    // the canvas-focus-mode class (toggled by the header button).  Clicking a
    // notification should always show the canvas, so switch it on when hidden.
    function revealCanvasWindow() {
        const canvasWindow = document.getElementById('additional-window');
        if (!canvasWindow) return;
        if (window.getComputedStyle(canvasWindow).display === 'none') {
            document.body.classList.add('canvas-focus-mode');
            // Let FullCalendar / other listeners re-measure the layout
            window.dispatchEvent(new Event('resize'));
        }
    }

    // ── Panel open / close ────────────────────────────────────────────────
    function openPanel() {
        panelOpen = true;
        panel.style.display = 'flex';
        btn.setAttribute('aria-expanded', 'true');
        fetchAndRender();
    }

    function closePanel() {
        panelOpen = false;
        panel.style.display = 'none';
        btn.setAttribute('aria-expanded', 'false');
    }

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        panelOpen ? closePanel() : openPanel();
    });

    // Close when clicking outside
    document.addEventListener('click', (e) => {
        if (panelOpen && !panel.contains(e.target) && e.target !== btn) {
            closePanel();
        }
    });

    // ── Mark all read ─────────────────────────────────────────────────────
    if (markAll) {
        markAll.addEventListener('click', async () => {
            try {
                await fetch('/api/notifications/read-all', { method: 'POST' });
                setCount(0);
                fetchAndRender();
            } catch (e) {}
        });
    }

    // ── SSE connection ────────────────────────────────────────────────────
    function connectSSE() {
        if (es) { es.close(); es = null; }

        es = new EventSource('/api/notifications/events');

        es.addEventListener('count', (e) => {
            try {
                const d = JSON.parse(e.data);
                setCount(d.unread || 0);
            } catch (_) {}
        });

        es.addEventListener('new', (e) => {
            try {
                const notif = JSON.parse(e.data.replace(/\\n/g, '\n'));
                // Increment badge
                setCount(unreadCount + 1);
                // If panel is open, refresh the list
                if (panelOpen) fetchAndRender();
                // Subtle bell animation
                btn.classList.add('notif-ring');
                setTimeout(() => btn.classList.remove('notif-ring'), 600);
                // Notify ai.js / canvas.js to auto-refresh if the user is
                // already looking at this project.
                if (notif.task_id) {
                    document.dispatchEvent(new CustomEvent('agent-callback-received', {
                        detail: {
                            taskId:     notif.task_id,
                            entityPath: notif.entity_path || '',
                        }
                    }));
                }
            } catch (_) {}
        });

        es.onerror = () => {
            // Reconnect after 10 s on error
            es.close();
            es = null;
            setTimeout(connectSSE, 10000);
        };
    }

    // Start SSE on page load
    connectSSE();

})();
