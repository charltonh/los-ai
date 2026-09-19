/**
 * Canvas Window — History Viewer + Streaming Terminal
 * Shows per-task AI/agent conversation history (expandable pairs) and live streaming output.
 */

(function() {
    'use strict';

    // --- State ---
    let currentTaskId = null;
    let currentEntityPath = '';
    let currentHistory = [];
    let activeStreamId = null;
    let activeEventSource = null;
    let streamOutputBuffer = '';
    let streamThinkingBuffer = '';
    // Track the task the STREAM belongs to (may differ from currentTaskId if user switches tasks)
    let streamTaskId = null;
    let streamEntityPath = '';
    let canvasMode = 'empty'; // 'empty' | 'history' | 'streaming'
    // Block to flash-highlight after the next render: {messageId, jobId}
    let pendingHighlight = null;
    // Direct callback — avoids CustomEvent dispatch issues entirely
    let _streamDoneCallback = null;

    // Track which pair indices are expanded
    const expandedPairs = new Set();

    // Duration of the notification flash-highlight (must match the CSS
    // animation on .canvas-pair-highlight)
    const HIGHLIGHT_MS = 3600;

    // --- DOM ---
    const canvasWindow  = document.getElementById('additional-window');
    const canvasTitle   = document.getElementById('canvas-title');
    const canvasContent = canvasWindow ? canvasWindow.querySelector('.window-content') : null;

    // Replace the static placeholder with our managed content
    function initCanvas() {
        if (!canvasContent) return;
        canvasContent.innerHTML = `
            <div id="canvas-body" class="canvas-body">
                <div id="canvas-empty-state" class="canvas-empty">
                    <i class="fas fa-file-alt"></i>
                    <p>Select a task to view its AI conversation history.</p>
                </div>
                <div id="canvas-history" class="canvas-history" style="display:none"></div>
                <div id="canvas-stream" class="canvas-stream" style="display:none">
                    <div id="canvas-stream-thinking" class="canvas-stream-thinking" style="display:none"></div>
                    <div id="canvas-stream-output" class="canvas-stream-output"></div>
                    <div id="canvas-stream-status" class="canvas-stream-status">
                        <span class="canvas-stream-spinner"><i class="fas fa-spinner fa-spin"></i></span>
                        <span id="canvas-stream-status-text">Waiting for response...</span>
                    </div>
                </div>
            </div>
        `;

        // Event delegation for expanding/collapsing pairs + copy/delete buttons
        const histEl = document.getElementById('canvas-history');
        if (histEl) {
            histEl.addEventListener('click', function(e) {
                // Handle copy buttons
                const copyBtn = e.target.closest('.canvas-copy-btn');
                if (copyBtn) {
                    e.stopPropagation();
                    handleCopy(copyBtn);
                    return;
                }

                // Handle delete buttons
                const deleteBtn = e.target.closest('.canvas-delete-btn');
                if (deleteBtn) {
                    e.stopPropagation();
                    handleDelete(deleteBtn);
                    return;
                }

                // Handle header toggle (but not when clicking inside body content)
                const header = e.target.closest('.canvas-pair-header');
                if (!header) return;
                // Don't toggle if the click was on a copy or delete button (already handled above)
                if (e.target.closest('.canvas-copy-btn')) return;
                if (e.target.closest('.canvas-delete-btn')) return;
                const pair = header.closest('.canvas-pair');
                if (!pair) return;
                const idx = parseInt(pair.dataset.pairIdx, 10);
                if (isNaN(idx)) return;
                togglePair(idx);
            });
        }
    }

    // --- Public API ---
    window.canvasAPI = {
        /**
         * Show a task's history in the canvas.
         * opts (optional) may carry {messageId, jobId} (or snake_case) taken
         * from a notification click — that block is expanded, scrolled to and
         * flash-highlighted once the history has rendered.
         */
        loadTask: function(taskId, entityPath, opts) {
            initCanvas();
            // If same task and streaming, don't interrupt
            if (activeStreamId && streamTaskId === taskId) return;

            // DON'T kill an active stream — let it complete and save in the background.
            // The stream uses streamTaskId (independent of currentTaskId).
            // Only close if the stream is truly orphaned (no stream id but eventSource open).
            if (!activeStreamId && activeEventSource) {
                activeEventSource.close();
                activeEventSource = null;
            }

            currentTaskId = taskId;
            currentEntityPath = entityPath || '';
            expandedPairs.clear();
            pendingHighlight = normalizeHighlight(opts);

            if (!taskId) { showEmpty(); return; }
            updateCanvasTitle(taskId, false, null, currentEntityPath);
            loadHistory(taskId, entityPath);
        },

        clearTask: function() {
            stopStream();
            initCanvas();
            currentTaskId = null;
            currentEntityPath = '';
            currentHistory = [];
            expandedPairs.clear();
            pendingHighlight = null;
            showEmpty();
            if (canvasTitle) canvasTitle.innerHTML = '<i class="fas fa-file-alt"></i> Canvas';
        },

        startStream: function(streamId, label, taskId, entityPath, configType, onDone) {
            if (taskId && taskId !== currentTaskId) {
                currentTaskId = taskId;
                currentEntityPath = entityPath || '';
            }
            activeStreamId = streamId;
            // Store the stream's own task context — won't change even if user navigates
            streamTaskId = taskId || currentTaskId;
            streamEntityPath = entityPath || currentEntityPath;
            streamOutputBuffer = '';
            streamThinkingBuffer = '';
            // Store direct callback — bypasses CustomEvent entirely
            _streamDoneCallback = (typeof onDone === 'function') ? onDone : null;

            const icon = configType === 'agent' ? '🦞' : '🧠';
            // Update title to show streaming — but keep the history visible (don't wipe it)
            updateCanvasTitle(currentTaskId, true, `${icon} ${label}`, currentEntityPath);
            // Don't call showStream() — history stays visible while the stream runs in background
            connectSSE(streamId, label, configType);
        },

        refreshHistory: function() {
            if (currentTaskId && canvasMode !== 'streaming') {
                loadHistory(currentTaskId, currentEntityPath);
            }
        },

        /**
         * Reload history AND auto-expand the last pair.
         * forTaskId: the task whose history was just saved (from ai.js doneHandler).
         * Only updates the canvas if forTaskId is what the user is currently viewing.
         * Called by ai.js after the assistant response has been saved.
         */
        refreshAndExpandLast: async function(forTaskId) {
            // Always try to refresh the target task if specified
            const targetId   = forTaskId || currentTaskId;
            const targetPath = (forTaskId === currentTaskId || !forTaskId)
                ? currentEntityPath : '';

            if (!targetId) return;

            // Fetch the history for the completed task
            try {
                const params = new URLSearchParams({ path: targetPath });
                const resp = await fetch(`/api/project/${encodeURIComponent(targetId)}/history?${params}`);
                const hist = resp.ok ? await resp.json() : [];

                // Only update the canvas display if the user is still viewing this task
                if (targetId === currentTaskId) {
                    currentHistory = hist;
                    canvasMode = 'history';
                    const pairs = buildPairs(currentHistory);
                    if (pairs.length > 0) {
                        expandedPairs.add(pairs.length - 1);
                    }
                    renderHistory();
                }
            } catch(e) {
                // Silently ignore — history display is non-critical
            }
        },

        getCurrentTaskId: function() { return currentTaskId; },
        getCurrentEntityPath: function() { return currentEntityPath; }
    };

    // --- History Loading ---
    async function loadHistory(taskId, entityPath) {
        try {
            const params = new URLSearchParams({ path: entityPath || '' });
            const resp = await fetch(`/api/project/${encodeURIComponent(taskId)}/history?${params}`);
            currentHistory = resp.ok ? await resp.json() : [];
        } catch (e) {
            currentHistory = [];
        }
        renderHistory();
    }

    // --- History Rendering (expandable pairs) ---
    function renderHistory() {
        const histEl = document.getElementById('canvas-history');
        if (!histEl) return;

        if (!currentHistory || currentHistory.length === 0) {
            pendingHighlight = null;   // nothing to reveal
            showEmpty();
            return;
        }

        showHistoryMode();

        // Build conversation pairs: [{user, assistant}]
        // A "pair" is a user message plus the immediately following assistant message.
        // Orphaned assistant messages (no preceding user) are also shown.
        const pairs = buildPairs(currentHistory);

        // Resolve which pair a notification click asked us to reveal
        let highlightIdx = -1;
        if (pendingHighlight) {
            for (let i = pairs.length - 1; i >= 0; i--) {
                if (pairMatchesHighlight(pairs[i])) { highlightIdx = i; break; }
            }
            // Expand it so the response content is visible, not just the header
            if (highlightIdx >= 0) expandedPairs.add(highlightIdx);
        }

        let html = '';
        let lastDate = '';

        pairs.forEach((pair, idx) => {
            const anchorMsg = pair.user || pair.assistant;
            const ts = anchorMsg && anchorMsg.timestamp ? new Date(anchorMsg.timestamp) : null;
            const dateStr = ts ? ts.toLocaleDateString(undefined, {weekday:'short', year:'numeric', month:'short', day:'numeric'}) : '';

            if (dateStr && dateStr !== lastDate) {
                html += `<div class="canvas-date-separator">${escHtml(dateStr)}</div>`;
                lastDate = dateStr;
            }

            const timeStr = ts ? ts.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'}) : '';
            const label   = anchorMsg ? (anchorMsg.label || '') : '';
            const ctype   = anchorMsg ? (anchorMsg.type || 'llm') : 'llm';

            // Summary line: first line of user prompt (or assistant content if orphan)
            const summarySource = pair.user ? pair.user.content : (pair.assistant ? pair.assistant.content : '');
            const summaryLine   = (summarySource || '').split('\n')[0].trim();
            const summary       = summaryLine.length > 120 ? summaryLine.substring(0, 120) + '…' : summaryLine;

            const hasResponse  = !!pair.assistant;
            const isExpanded   = expandedPairs.has(idx);
            const toggleArrow  = hasResponse ? `<span class="canvas-pair-arrow">${isExpanded ? '▲' : '▼'}</span>` : '';
            const labelBadge   = label ? `<span class="canvas-label-badge">${escHtml(label)}</span>` : '';

            // ── Header (always visible, clickable if there's a response) ──
            // When expanded with a user prompt, show the full prompt in the header
            const deleteBtn = isExpanded
                ? `<button class="canvas-delete-btn" data-delete-idx="${idx}" title="Delete this entry"><i class="fas fa-trash"></i></button>`
                : '';
            const headerPrompt = isExpanded && pair.user && pair.user.content
                ? `<span class="canvas-pair-summary canvas-pair-summary-expanded">${formatContent(pair.user.content)}</span>
                   <button class="canvas-copy-btn" data-copy-idx="${idx}" data-copy-role="user" title="Copy prompt">📋 Copy</button>`
                : `<span class="canvas-pair-summary">${escHtml(summary)}</span>`;

            html += `
                <div class="canvas-pair${isExpanded ? ' canvas-pair-expanded' : ''}${idx === highlightIdx ? ' canvas-pair-highlight' : ''}" data-pair-idx="${idx}">
                    <div class="canvas-pair-header${hasResponse ? ' canvas-pair-clickable' : ''}">
                        <span class="canvas-pair-role-icon">👤</span>
                        ${headerPrompt}
                        ${!isExpanded ? labelBadge : ''}
                        <span class="canvas-pair-time">${escHtml(timeStr)}</span>
                        ${toggleArrow}
                        ${deleteBtn}
                    </div>
            `;

            // ── Expanded body ──
            if (isExpanded) {
                html += `<div class="canvas-pair-body">`;

                // Full assistant response (user prompt is now in the header)
                if (pair.assistant) {
                    const aIcon  = ctype === 'agent' ? '🦞' : '🧠';
                    const aLabel = pair.assistant.label || label;
                    const rc     = pair.assistant.returncode;
                    const rcBadge = (rc != null && rc !== 0)
                        ? `<span class="canvas-rc-error">exit ${rc}</span>` : '';
                    const thinking = pair.assistant.thinking;

                    html += `
                        <div class="canvas-pair-section canvas-pair-section-assistant">
                            <div class="canvas-pair-section-label">
                                ${aIcon} AI Response
                                ${aLabel ? `<span class="canvas-label-badge" style="margin-left:0.3rem">${escHtml(aLabel)}</span>` : ''}
                                ${rcBadge}
                                <button class="canvas-copy-btn" data-copy-idx="${idx}" data-copy-role="assistant" title="Copy response">📋 Copy</button>
                            </div>
                            ${thinking ? `
                                <details class="canvas-thinking-details">
                                    <summary>Thinking…</summary>
                                    <pre class="canvas-code" style="opacity:0.7">${escHtml(thinking.trim())}</pre>
                                </details>
                            ` : ''}
                            <div class="canvas-pair-section-content canvas-response-text">${formatContent(pair.assistant.content)}</div>
                        </div>
                    `;
                } else if (!pair.user) {
                    html += `<div class="canvas-pair-pending">⏳ Response pending…</div>`;
                }

                html += `</div>`; // canvas-pair-body
            }

            html += `</div>`; // canvas-pair
        });

        histEl.innerHTML = html;

        // If no pairs expanded yet, auto-expand the last one
        if (expandedPairs.size === 0 && pairs.length > 0) {
            // Don't auto-expand — let the user click
        }

        if (highlightIdx >= 0) {
            // Bring the requested block into view; the CSS animation on
            // .canvas-pair-highlight fades the highlight out on its own.
            const target = histEl.querySelector(`.canvas-pair[data-pair-idx="${highlightIdx}"]`);
            if (target) {
                // scrollIntoView with block:'center' works for both the canvas
                // scroll container and the page, so the block is always visible.
                try {
                    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
                } catch (e) {
                    target.scrollIntoView();
                }
                // Drop the class once the flash animation has finished so a
                // later re-render (e.g. SSE refresh) starts clean.
                window.setTimeout(function() {
                    target.classList.remove('canvas-pair-highlight');
                }, HIGHLIGHT_MS);
            }
        } else {
            histEl.scrollTop = histEl.scrollHeight;
        }

        // The highlight applies to this render only
        pendingHighlight = null;
    }

    /**
     * Normalise a highlight request coming from a notification click.
     * Accepts camelCase or snake_case keys; returns null when nothing to match.
     */
    function normalizeHighlight(opts) {
        if (!opts || typeof opts !== 'object') return null;
        const messageId = opts.messageId || opts.message_id || '';
        const jobId     = opts.jobId || opts.job_id || '';
        if (!messageId && !jobId) return null;
        return { messageId: String(messageId), jobId: String(jobId) };
    }

    /** True if either message of a pair matches the pending highlight. */
    function pairMatchesHighlight(pair) {
        if (!pendingHighlight) return false;
        return [pair.user, pair.assistant].some(function(m) {
            if (!m) return false;
            if (pendingHighlight.messageId && m.id === pendingHighlight.messageId) return true;
            if (pendingHighlight.jobId && m.job_id === pendingHighlight.jobId) return true;
            return false;
        });
    }

    // Build pairs from flat history array
    function buildPairs(history) {
        const pairs = [];
        let i = 0;
        while (i < history.length) {
            const msg = history[i];
            if (msg.role === 'user') {
                const pair = { user: msg, assistant: null };
                if (i + 1 < history.length && history[i + 1].role === 'assistant') {
                    pair.assistant = history[i + 1];
                    i += 2;
                } else {
                    i += 1;
                }
                pairs.push(pair);
            } else if (msg.role === 'assistant') {
                // Orphaned assistant response
                pairs.push({ user: null, assistant: msg });
                i += 1;
            } else {
                i += 1;
            }
        }
        return pairs;
    }

    // Toggle expand/collapse of a pair by re-rendering
    function togglePair(idx) {
        if (expandedPairs.has(idx)) {
            expandedPairs.delete(idx);
        } else {
            expandedPairs.add(idx);
        }
        renderHistory();
    }

    // Delete a conversation pair from history after confirmation
    async function handleDelete(btn) {
        const idx = parseInt(btn.dataset.deleteIdx, 10);
        if (isNaN(idx) || !currentHistory || !currentTaskId) return;

        const pairs = buildPairs(currentHistory);
        if (idx >= pairs.length) return;

        const pair = pairs[idx];
        const summaryLine = (pair.user ? pair.user.content : (pair.assistant ? pair.assistant.content : '')).split('\n')[0].trim();
        const preview = summaryLine.length > 60 ? summaryLine.substring(0, 60) + '…' : summaryLine;

        if (!confirm(`Delete this conversation entry?\n\n"${preview}"\n\nThis cannot be undone.`)) {
            return;
        }

        // Compute the start index and count in the flat history array
        let startIdx = 0;
        let count = 0;
        let pairIdx = 0;
        let i = 0;
        while (i < currentHistory.length) {
            const msg = currentHistory[i];
            if (msg.role === 'user') {
                if (pairIdx === idx) {
                    startIdx = i;
                    count = 1;
                    if (i + 1 < currentHistory.length && currentHistory[i + 1].role === 'assistant') {
                        count = 2;
                    }
                    break;
                }
                if (i + 1 < currentHistory.length && currentHistory[i + 1].role === 'assistant') {
                    i += 2;
                } else {
                    i += 1;
                }
                pairIdx++;
            } else if (msg.role === 'assistant') {
                if (pairIdx === idx) {
                    startIdx = i;
                    count = 1;
                    break;
                }
                i += 1;
                pairIdx++;
            } else {
                i += 1;
            }
        }

        if (count === 0) {
            console.warn('[canvas] Could not locate pair in flat history');
            return;
        }

        try {
            const params = new URLSearchParams({ path: currentEntityPath || '' });
            const resp = await fetch(`/api/project/${encodeURIComponent(currentTaskId)}/history/entry?${params}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ start_idx: startIdx, count: count })
            });
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                console.error('[canvas] delete failed:', data.error || resp.statusText);
                alert('Failed to delete entry: ' + (data.error || 'Server error'));
                return;
            }
            // Remove from local state and re-render
            currentHistory.splice(startIdx, count);
            expandedPairs.delete(idx);
            // Adjust expanded indices above the deleted pair
            const newExpanded = new Set();
            expandedPairs.forEach(function(oldIdx) {
                if (oldIdx < idx) {
                    newExpanded.add(oldIdx);
                } else if (oldIdx > idx) {
                    newExpanded.add(oldIdx - 1);
                }
            });
            expandedPairs.clear();
            newExpanded.forEach(function(v) { expandedPairs.add(v); });
            renderHistory();
        } catch (e) {
            console.error('[canvas] delete error:', e);
            alert('Failed to delete entry. See console for details.');
        }
    }

    // Copy raw text from a prompt/response section to clipboard
    function handleCopy(btn) {
        const idx  = parseInt(btn.dataset.copyIdx, 10);
        const role = btn.dataset.copyRole; // 'user' or 'assistant'
        if (isNaN(idx) || !currentHistory) return;

        const pairs = buildPairs(currentHistory);
        if (idx >= pairs.length) return;

        const pair = pairs[idx];
        let rawText = '';
        if (role === 'user' && pair.user && pair.user.content) {
            rawText = pair.user.content;
        } else if (role === 'assistant' && pair.assistant && pair.assistant.content) {
            rawText = pair.assistant.content;
        }

        if (!rawText) return;

        navigator.clipboard.writeText(rawText).then(function() {
            const orig = btn.innerHTML;
            btn.innerHTML = '✅ Copied!';
            btn.style.color = '#47b881';
            btn.style.fontWeight = '600';
            setTimeout(function() {
                btn.innerHTML = orig;
                btn.style.color = '';
                btn.style.fontWeight = '';
            }, 1500);
        }).catch(function(err) {
            console.warn('[canvas] copy failed:', err);
            btn.innerHTML = '❌ Failed';
            setTimeout(function() { btn.innerHTML = '📋 Copy'; }, 1500);
        });
    }

    // Format message content for display (markdown-lite)
    function formatContent(text) {
        if (!text) return '<em style="opacity:0.4">(empty)</em>';
        let escaped = escHtml(text);
        // Fenced code blocks
        escaped = escaped.replace(/```([^`]*?)```/gs, '<pre class="canvas-code">$1</pre>');
        // Inline code
        escaped = escaped.replace(/`([^`\n]+)`/g, '<code>$1</code>');
        // Bold **text**
        escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        // Line breaks
        escaped = escaped.replace(/\n/g, '<br>');
        return escaped;
    }

    // --- Streaming ---
    function connectSSE(streamId, label, configType) {
        if (activeEventSource) activeEventSource.close();

        const outEl    = document.getElementById('canvas-stream-output');
        const thinkEl  = document.getElementById('canvas-stream-thinking');
        const statusEl = document.getElementById('canvas-stream-status-text');

        if (outEl) outEl.innerHTML = '';
        if (thinkEl) { thinkEl.style.display = 'none'; thinkEl.innerHTML = ''; }
        if (statusEl) statusEl.textContent = 'Connecting...';

        const es = new EventSource(`/api/ai/stream/${streamId}`);
        activeEventSource = es;

        es.addEventListener('open', function() {
            console.log('[canvas] SSE opened | streamId=', streamId);
        });

        let outputEventCount = 0;
        es.addEventListener('output', function(e) {
            outputEventCount++;
            console.log('[canvas] output event #' + outputEventCount + ' | len=', e.data.length, '| data=', e.data.substring(0, 60));
            const line = e.data.replace(/\\n/g, '\n');
            streamOutputBuffer += line + '\n';
            if (outEl) {
                const span = document.createElement('span');
                span.className = 'canvas-stream-line';
                span.textContent = line;
                outEl.appendChild(span);
                outEl.appendChild(document.createElement('br'));
                outEl.scrollTop = outEl.scrollHeight;
            }
            if (statusEl) statusEl.textContent = 'Receiving output…';
        });

        es.addEventListener('thinking', function(e) {
            // Buffer debug/thinking output but don't display it in the canvas
            // (debug logs go to aicall.log — visible in AI window if 'Show Thinking' is on)
            const line = e.data.replace(/\\n/g, '\n');
            streamThinkingBuffer += line + '\n';
        });

        es.addEventListener('done', function(e) {
            const returncode = parseInt(e.data, 10);
            if (statusEl) {
                statusEl.textContent = returncode === 0 ? '✓ Complete' : `⚠ Exited (code ${returncode})`;
            }
            const spinner = document.querySelector('.canvas-stream-spinner');
            if (spinner) spinner.style.display = 'none';

            es.close();
            activeEventSource = null;
            activeStreamId = null;

            // Use streamTaskId — the task this stream belongs to, even if user navigated away
            const doneForTask   = streamTaskId;
            const doneForPath   = streamEntityPath;
            streamTaskId    = null;
            streamEntityPath = '';

            console.log('[canvas] stream done | rc=', returncode,
                '| bufLen=', streamOutputBuffer.length,
                '| taskId=', doneForTask,
                '| hasCb=', !!_streamDoneCallback);

            // PRIMARY: call direct callback (avoids CustomEvent timing issues entirely)
            if (_streamDoneCallback) {
                const cb = _streamDoneCallback;
                _streamDoneCallback = null;
                cb(streamOutputBuffer, streamThinkingBuffer, returncode, doneForTask, doneForPath);
            } else {
                // FALLBACK: CustomEvent for any legacy listeners
                document.dispatchEvent(new CustomEvent('canvas-stream-done', {
                    detail: {
                        output: streamOutputBuffer,
                        thinking: streamThinkingBuffer,
                        returncode: returncode,
                        taskId: doneForTask,
                        entityPath: doneForPath
                    }
                }));
            }

            // Update title for the current view (might be different from the completed stream's task)
            updateCanvasTitle(currentTaskId, false, null, currentEntityPath);
        });

        es.addEventListener('error', function(e) {
            const msg = e.data ? e.data.replace(/\\n/g, '\n') : 'Connection error';
            if (statusEl) statusEl.textContent = `Error: ${msg}`;
            const spinner = document.querySelector('.canvas-stream-spinner');
            if (spinner) spinner.style.display = 'none';
            es.close();
            activeEventSource = null;
            activeStreamId = null;
        });

        es.onerror = function() {
            // This fires on connection-level errors (not named 'error' SSE events).
            // If the connection closed prematurely and we have buffered output, still save it.
            if (es.readyState === EventSource.CLOSED) {
                console.warn('[canvas] EventSource closed unexpectedly | bufLen=', streamOutputBuffer.length);
                if (statusEl) statusEl.textContent = 'Connection closed';
                const hadOutput = streamOutputBuffer.length > 0;
                const savedForTask = streamTaskId;
                const savedForPath = streamEntityPath;
                activeEventSource = null;
                activeStreamId = null;
                streamTaskId = null;
                streamEntityPath = '';

                if (hadOutput) {
                    // Fire callback (or CustomEvent fallback) with buffered output
                    const cb = _streamDoneCallback;
                    _streamDoneCallback = null;
                    if (cb) {
                        cb(streamOutputBuffer, streamThinkingBuffer, -1, savedForTask, savedForPath);
                    } else {
                        document.dispatchEvent(new CustomEvent('canvas-stream-done', {
                            detail: {
                                output: streamOutputBuffer,
                                thinking: streamThinkingBuffer,
                                returncode: -1,
                                taskId: savedForTask,
                                entityPath: savedForPath
                            }
                        }));
                    }
                }
                updateCanvasTitle(currentTaskId, false, null, currentEntityPath);
            }
        };

        if (statusEl) statusEl.textContent = 'Streaming…';
    }

    function stopStream() {
        if (activeEventSource) { activeEventSource.close(); activeEventSource = null; }
        activeStreamId = null;
    }

    // --- Mode Switching ---
    function showEmpty() {
        canvasMode = 'empty';
        el('canvas-empty-state',  'flex');
        el('canvas-history',      'none');
        el('canvas-stream',       'none');
        hideCanvasHeaderControls();
    }

    function showHistoryMode() {
        canvasMode = 'history';
        el('canvas-empty-state',  'none');
        el('canvas-history',      'block');
        el('canvas-stream',       'none');
        hideCanvasHeaderControls();
    }

    function showStream() {
        canvasMode = 'streaming';
        el('canvas-empty-state',  'none');
        el('canvas-history',      'none');
        el('canvas-stream',       'flex');
        const spinner = document.querySelector('.canvas-stream-spinner');
        if (spinner) spinner.style.display = 'inline';
        hideCanvasHeaderControls();
    }

    function hideCanvasHeaderControls() {
        const controls = document.querySelector('.canvas-header-controls');
        if (controls) controls.style.visibility = 'hidden';
    }

    function el(id, display) {
        const e = document.getElementById(id);
        if (e) e.style.display = display;
    }

    // --- Title ---
    function updateCanvasTitle(taskId, isStreaming, streamLabel, entityPath) {
        if (!canvasTitle) return;
        if (!taskId) {
            canvasTitle.innerHTML = '<i class="fas fa-file-alt"></i> Canvas';
            return;
        }
        const shortId = taskId.length > 42 ? taskId.substring(0, 42) + '…' : taskId;
        const entityPart = entityPath ? ` <span class="canvas-title-entity">[${escHtml(entityPath)}]</span>` : '';
        if (isStreaming && streamLabel) {
            canvasTitle.innerHTML = `<span class="canvas-title-project">project/${escHtml(shortId)}</span>${entityPart}<span class="canvas-title-running"> ${escHtml(streamLabel)} <i class="fas fa-spinner fa-spin"></i></span>`;
        } else {
            canvasTitle.innerHTML = `<span class="canvas-title-project">project/${escHtml(shortId)}</span>${entityPart}`;
        }
    }

    // --- Helpers ---
    function escHtml(text) {
        const d = document.createElement('div');
        d.textContent = String(text);
        return d.innerHTML;
    }

    // --- Listen for task events from editor.js / notifications.js ---
    document.addEventListener('canvas-task-selected', function(e) {
        const d = e.detail || {};
        // Pass the whole detail as opts so a notification click can ask for a
        // specific block to be revealed and flash-highlighted.
        window.canvasAPI.loadTask(d.taskId, d.entityPath, d);
    });

    document.addEventListener('canvas-task-cleared', function() {
        window.canvasAPI.clearTask();
    });

    // --- Init ---
    document.addEventListener('DOMContentLoaded', function() {
        initCanvas();
        showEmpty();
    });

})();
