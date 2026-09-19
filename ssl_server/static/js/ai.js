/**
 * AI Window functionality for the LOS Dashboard
 * - Per-task conversation history (saved via canvas → history.json)
 * - Streaming output via SSE (Server-Sent Events)
 * - AI output always shows the full current/last response
 */

document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const aiConfigSelect  = document.getElementById('ai-config-select');
    const aiPromptInput   = document.getElementById('ai-prompt-input');
    const aiOutput        = document.getElementById('ai-output');
    const aiSendBtn       = document.getElementById('ai-send-btn');
    const aiClearBtn      = document.getElementById('ai-clear-btn');
    const aiShowThinking  = document.getElementById('ai-show-thinking');
    // Custom AI-config dropdown elements
    const customSelectEl  = document.getElementById('ai-custom-select');
    const customTriggerEl = document.getElementById('ai-select-trigger');
    const customOptionsEl = document.getElementById('ai-select-options');
    const triggerIconSlot = document.getElementById('ai-trigger-icon-slot');
    const triggerLabelEl  = document.getElementById('ai-trigger-label');
    // Context dropdown elements
    const ctxSelectEl     = document.getElementById('ai-context-custom-select');
    const ctxTriggerEl    = document.getElementById('ai-context-trigger');
    const ctxOptionsEl    = document.getElementById('ai-context-options');
    const ctxTriggerLabel = document.getElementById('ai-context-trigger-label');
    const ctxEntityPathEl = document.getElementById('ai-entity-path');

    // State
    let aiConfigs       = [];
    let _allConfigs     = [];   // full config list, kept in sync with the hidden <select>
    let isAiLoading     = false;
    let currentTaskId   = null;
    let currentTaskPath = '';
    let lastUsedLabel   = null;
    // Fallback: holds the streamDoneCallback when canvas-stream-done CustomEvent fires
    // (used when _streamDoneCallback was cleared before 'done' arrived)
    let _pendingStreamDone = null;

    // ── Context dropdown state ─────────────────────────────────────────
    // 'task' (default) | 'entity' | 'none'
    let contextMode               = 'task';
    let currentAgendaPath         = '';   // the currently-selected agenda entity (from sidebar)
    let currentAgendaName         = '';   // name of that entity
    let currentSubEntityName      = '';   // '' when in 'entity' mode at the root, else the sub-entity name
    let subEntities               = [];   // [{name, weight}, ...] children of currentAgendaPath (weight-desc)
    // Cache of the last task selection so re-entering 'task' mode can restore it
    let cachedTaskSelection       = null; // {taskId, entityPath, taskType, eventData?}

    // Initialize
    loadAiConfigs();
    setupAiEventListeners();
    listenForEntityChanges();
    listenForStreamDone();
    listenForAgentCallback();
    // Set up the Context dropdown — fetch initial subentities (root) and render
    buildContextOptions();
    syncContextTrigger();
    loadSubEntities('');  // populate subentities for the root by default


    // ─── Load AI Configurations ───────────────────────────────────────────────

    async function loadAiConfigs() {
        try {
            const response = await fetch('/api/ai/configs/full');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            aiConfigs = await response.json();
            populateDropdown(aiConfigs);
        } catch (error) {
            console.error('Error loading AI configs:', error);
            try {
                const r2 = await fetch('/api/ai/configs');
                if (r2.ok) {
                    aiConfigs = await r2.json();
                    populateDropdown(aiConfigs);
                }
            } catch (e2) {
                if (aiConfigSelect) aiConfigSelect.innerHTML = '<option value="">Error loading configs</option>';
            }
        }
    }

    function populateDropdown(configs) {
        if (!aiConfigSelect) return;
        _allConfigs = configs || [];
        aiConfigSelect.innerHTML = '';

        if (!configs || configs.length === 0) {
            aiConfigSelect.innerHTML = '<option value="">No AI configs found</option>';
            if (triggerLabelEl) triggerLabelEl.textContent = 'No AI configs';
            return;
        }

        configs.forEach((cfg, idx) => {
            const option = document.createElement('option');
            option.value = cfg.label;
            const isAgent = cfg.config_type === 'agent' || cfg.type === 'agent';
            option.dataset.configType = isAgent ? 'agent' : 'llm';
            option.textContent = cfg.label;   // hidden, text doesn't matter
            aiConfigSelect.appendChild(option);
        });

        if (aiConfigSelect.options.length > 0) aiConfigSelect.selectedIndex = 0;
        buildCustomOptions(configs);
        syncTrigger();
    }

    /** Returns true if the icon value looks like an image path (SVG, PNG, etc.) */
    function isImagePath(icon) {
        if (!icon) return false;
        return icon.startsWith('/') || icon.startsWith('http') ||
               /\.(svg|png|jpg|jpeg|webp|gif|ico)$/i.test(icon);
    }

    /** Human-readable label: underscores → spaces, colons → ' › ' */
    function fmtLabel(label) {
        return label.replace(/_/g, ' ').replace(/:/g, ' › ');
    }

    /**
     * Build the icon element for an option (img or emoji span).
     * @param {object} cfg   — config entry
     * @param {string} cls   — CSS class to apply
     */
    function buildIconEl(cfg, cls) {
        const isAgent = cfg.config_type === 'agent' || cfg.type === 'agent';
        const icon = cfg.icon || '';
        if (isImagePath(icon)) {
            const img = document.createElement('img');
            img.className = cls;
            img.src = icon;
            img.alt = cfg.label;
            return img;
        }
        const span = document.createElement('span');
        span.className = cls + '-emoji';
        // Use icon as emoji if it's not a FontAwesome class
        span.textContent = (icon && !icon.startsWith('fas ') && !icon.startsWith('fa-'))
            ? icon : (isAgent ? '🦞' : '🧠');
        return span;
    }

    /** Build the list of .ai-opt divs inside the dropdown panel */
    function buildCustomOptions(configs) {
        if (!customOptionsEl) return;
        customOptionsEl.innerHTML = '';
        configs.forEach((cfg, idx) => {
            const item = document.createElement('div');
            item.className = 'ai-opt';
            item.setAttribute('role', 'option');
            item.dataset.value = cfg.label;
            item.dataset.index = String(idx);

            item.appendChild(buildIconEl(cfg, 'ai-opt-icon'));

            const lbl = document.createElement('span');
            lbl.className = 'ai-opt-label';
            lbl.textContent = fmtLabel(cfg.label);
            item.appendChild(lbl);

            // Tooltip
            const tips = [];
            if (cfg.aiconfig) tips.push(`Config: ${cfg.aiconfig.split('/').pop()}`);
            if (cfg.model)    tips.push(`Model: ${cfg.model}`);
            if (cfg.provider) tips.push(`Provider: ${cfg.provider}`);
            if (tips.length)  item.title = tips.join(' | ');

            item.addEventListener('click', () => {
                if (aiConfigSelect) {
                    aiConfigSelect.selectedIndex = idx;
                    aiConfigSelect.dispatchEvent(new Event('change'));
                }
                syncTrigger();
                closeCustomDropdown();
            });

            customOptionsEl.appendChild(item);
        });
    }

    /** Sync the trigger button to show the currently selected option's icon + label */
    function syncTrigger() {
        if (!aiConfigSelect || !triggerIconSlot || !triggerLabelEl) return;
        const idx = aiConfigSelect.selectedIndex;

        // Update selected highlight in option list
        if (customOptionsEl) {
            customOptionsEl.querySelectorAll('.ai-opt').forEach((o, i) =>
                o.classList.toggle('selected', i === idx));
        }

        if (idx < 0 || idx >= _allConfigs.length) {
            triggerIconSlot.innerHTML = '';
            triggerLabelEl.textContent = 'Select AI…';
            return;
        }
        const cfg = _allConfigs[idx];
        triggerIconSlot.innerHTML = '';
        triggerIconSlot.appendChild(buildIconEl(cfg, 'ai-trigger-icon'));
        triggerLabelEl.textContent = fmtLabel(cfg.label);
    }

    function openCustomDropdown()  { if (customSelectEl) customSelectEl.classList.add('open'); }
    function closeCustomDropdown() { if (customSelectEl) customSelectEl.classList.remove('open'); }
    function toggleCustomDropdown() {
        if (customSelectEl && customSelectEl.classList.contains('open')) closeCustomDropdown();
        else openCustomDropdown();
    }

    function setDefaultAiConfig(label) {
        if (!aiConfigSelect) return false;
        for (let i = 0; i < aiConfigSelect.options.length; i++) {
            if (aiConfigSelect.options[i].value === label) {
                aiConfigSelect.selectedIndex = i;
                syncTrigger();
                return true;
            }
        }
        // Try partial match (last segment)
        const labelLower = label.toLowerCase();
        for (let i = 0; i < aiConfigSelect.options.length; i++) {
            const optLabel = aiConfigSelect.options[i].value.toLowerCase();
            const lastSegment = optLabel.split(':').pop();
            if (lastSegment === labelLower) {
                aiConfigSelect.selectedIndex = i;
                syncTrigger();
                return true;
            }
        }
        if (aiConfigSelect.options.length > 0) { aiConfigSelect.selectedIndex = 0; syncTrigger(); }
        return false;
    }

    function getSelectedConfigMeta() {
        if (!aiConfigSelect) return {};
        const selected = aiConfigSelect.options[aiConfigSelect.selectedIndex];
        if (!selected) return {};
        return aiConfigs.find(c => c.label === selected.value) || {};
    }

    // ─── Event Listeners ──────────────────────────────────────────────────────

    function setupAiEventListeners() {
        if (aiSendBtn) aiSendBtn.addEventListener('click', sendAiPrompt);
        if (aiClearBtn) aiClearBtn.addEventListener('click', clearAiWindow);

        if (aiPromptInput) {
            aiPromptInput.addEventListener('keydown', (e) => {
                if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); sendAiPrompt(); }
            });
        }

        if (aiShowThinking) {
            aiShowThinking.addEventListener('change', () => {
                const thinkEl = document.getElementById('ai-thinking-section');
                if (thinkEl) thinkEl.style.display = aiShowThinking.checked ? 'block' : 'none';
            });
        }

        // Keep hidden select in sync with custom dropdown
        if (aiConfigSelect) {
            aiConfigSelect.addEventListener('change', syncTrigger);
        }

        // Custom dropdown: trigger click toggles open/close
        if (customTriggerEl) {
            customTriggerEl.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleCustomDropdown();
            });
            customTriggerEl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCustomDropdown(); }
                if (e.key === 'Escape') closeCustomDropdown();
            });
        }

        // Close custom dropdown when clicking outside it
        document.addEventListener('click', (e) => {
            if (customSelectEl && !customSelectEl.contains(e.target)) closeCustomDropdown();
        });

        // ── Context dropdown click handling ──
        if (ctxTriggerEl) {
            ctxTriggerEl.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleContextDropdown();
            });
            ctxTriggerEl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleContextDropdown(); }
                if (e.key === 'Escape') closeContextDropdown();
            });
        }
        document.addEventListener('click', (e) => {
            if (ctxSelectEl && !ctxSelectEl.contains(e.target)) closeContextDropdown();
        });
    }

    // ─── Context dropdown helpers ─────────────────────────────────────────────

    function openContextDropdown()   { if (ctxSelectEl) ctxSelectEl.classList.add('open'); }
    function closeContextDropdown()  { if (ctxSelectEl) ctxSelectEl.classList.remove('open'); }
    function toggleContextDropdown() {
        if (ctxSelectEl && ctxSelectEl.classList.contains('open')) closeContextDropdown();
        else openContextDropdown();
    }

    /**
     * (Re)build the option rows inside the Context dropdown panel.
     * Order:  Task  /  Entity  /  None    [separator]   Entity {sub1}  Entity {sub2}  ...
     * Sub-entities are sourced from `subEntities` (already weight-sorted DESC by the server).
     */
    function buildContextOptions() {
        if (!ctxOptionsEl) return;
        ctxOptionsEl.innerHTML = '';

        const fixed = [
            { value: 'task',   label: 'Task'   },
            { value: 'entity', label: 'Entity' },
            { value: 'none',   label: 'None'   },
        ];
        fixed.forEach(opt => ctxOptionsEl.appendChild(makeContextOpt(opt.value, opt.label)));

        if (subEntities.length > 0) {
            const sep = document.createElement('div');
            sep.className = 'ai-opt-separator';
            ctxOptionsEl.appendChild(sep);
            subEntities.forEach(sub => {
                ctxOptionsEl.appendChild(
                    makeContextOpt('subentity:' + sub.name, 'Entity ' + sub.name)
                );
            });
        }
    }

    function makeContextOpt(value, label) {
        const item = document.createElement('div');
        item.className = 'ai-opt';
        item.setAttribute('role', 'option');
        item.dataset.value = value;
        const span = document.createElement('span');
        span.className = 'ai-opt-label';
        span.textContent = label;
        item.appendChild(span);
        item.addEventListener('click', () => {
            if (value === 'task' || value === 'entity' || value === 'none') {
                setContextMode(value, '');
            } else if (value.startsWith('subentity:')) {
                setContextMode('entity', value.substring('subentity:'.length));
            }
            closeContextDropdown();
        });
        return item;
    }

    /** Update the visible trigger label, selected highlight, and entity-path text. */
    function syncContextTrigger() {
        if (!ctxTriggerLabel) return;
        if (contextMode === 'task')      ctxTriggerLabel.textContent = 'Task';
        else if (contextMode === 'none') ctxTriggerLabel.textContent = 'None';
        else if (contextMode === 'entity') {
            ctxTriggerLabel.textContent = currentSubEntityName
                ? 'Entity ' + currentSubEntityName
                : 'Entity';
        }
        // Mark the selected option
        if (ctxOptionsEl) {
            ctxOptionsEl.querySelectorAll('.ai-opt').forEach(opt => {
                const v = opt.dataset.value;
                const isSel =
                    (v === contextMode && contextMode !== 'entity') ||
                    (contextMode === 'entity' && !currentSubEntityName && v === 'entity') ||
                    (contextMode === 'entity' && currentSubEntityName &&
                        v === 'subentity:' + currentSubEntityName);
                opt.classList.toggle('selected', isSel);
            });
        }
        // Update the entity-path label shown beside the dropdown.
        // Show full path (with sub-entity suffix) only when we are in entity mode
        // and the path is not the user root (empty path == root agenda).
        if (ctxEntityPathEl) {
            if (contextMode === 'entity') {
                const full = getActiveEntityPath();
                ctxEntityPathEl.textContent = full || '';
            } else {
                ctxEntityPathEl.textContent = '';
            }
        }
    }

    /** Combine the currently-selected agenda path with the chosen sub-entity name. */
    function getActiveEntityPath() {
        const base = currentAgendaPath || '';
        if (currentSubEntityName) {
            return base ? base + '/' + currentSubEntityName : currentSubEntityName;
        }
        return base;
    }

    /** Fetch the sub-entities (ordered by weight desc) for the given entity path. */
    async function loadSubEntities(entityPath) {
        try {
            const resp = await fetch('/api/agenda/subentities?path=' + encodeURIComponent(entityPath));
            if (!resp.ok) {
                console.warn('[ctx] loadSubEntities non-ok:', resp.status, resp.statusText);
                subEntities = [];
            } else {
                const data = await resp.json();
                // Guard: endpoint should return an array
                subEntities = Array.isArray(data) ? data : [];
                console.log('[ctx] subEntities for "' + entityPath + '":', subEntities);
            }
        } catch (e) {
            console.error('[ctx] loadSubEntities error:', e);
            subEntities = [];
        }
        buildContextOptions();
        syncContextTrigger();
    }

    /**
     * Switch context mode.
     *  - 'task'   : restore the cached task selection (if any), keep canvas in sync.
     *  - 'entity' : clear the Task Editor (silently), load the entity's history into the canvas,
     *               and (if subName given) target a sub-entity of the current agenda.
     *  - 'none'   : clear the Task Editor and the canvas (no AI context attached).
     */
    function setContextMode(mode, subName = '') {
        contextMode = mode;
        currentSubEntityName = (mode === 'entity') ? (subName || '') : '';
        syncContextTrigger();
        applyContextMode();
    }

    // Expose globally so notification clicks can switch AI context to Entity mode
    window.setAiContextMode = setContextMode;

    function applyContextMode() {
        if (contextMode === 'task') {
            // Restore the previously selected task, if we have one.
            if (cachedTaskSelection && cachedTaskSelection.taskId) {
                document.dispatchEvent(new CustomEvent('restore-task-selection', {
                    detail: cachedTaskSelection
                }));
            } else {
                // Otherwise just make sure the canvas is empty.
                document.dispatchEvent(new CustomEvent('canvas-task-cleared'));
                currentTaskId = null;
                currentTaskPath = '';
                setAiOutputPlaceholder();
            }
        } else if (contextMode === 'entity') {
            const entityPath = getActiveEntityPath();
            // Wipe the editor without telling the canvas to clear — we'll load
            // the entity history into the canvas next.
            document.dispatchEvent(new CustomEvent('clear-task-editor-only'));
            // The history.json for an entity lives at:
            //   <entity>/data/project/entity/history.json
            // so we use the literal task_id "entity" with the entity's relative path.
            currentTaskId   = 'entity';
            currentTaskPath = entityPath;
            document.dispatchEvent(new CustomEvent('canvas-task-selected', {
                detail: { taskId: 'entity', entityPath: entityPath }
            }));
            // Load the entity's conversation history to set the AI dropdown
            // to the last actual AI used and show the last response.
            loadTaskHistory('entity', entityPath);
        } else {  // 'none'
            document.dispatchEvent(new CustomEvent('clear-task-editor-only'));
            document.dispatchEvent(new CustomEvent('canvas-task-cleared'));
            currentTaskId = null;
            currentTaskPath = '';
            setAiOutputPlaceholder();
        }
    }


    // ─── Entity / Task Selection ──────────────────────────────────────────────

    function listenForEntityChanges() {
        document.addEventListener('entity-selected', async (e) => {
            const detail = e.detail || {};
            const entityPath = detail.path || '';
            currentAgendaPath = entityPath;
            currentAgendaName = detail.name || '';
            // Refresh the sub-entity list for the new agenda.
            await loadSubEntities(entityPath);
            // If the previously-selected sub-entity no longer exists in this
            // agenda's children, fall back to the agenda itself (still 'Entity'
            // mode, just without a sub-entity).
            if (contextMode === 'entity' && currentSubEntityName &&
                !subEntities.some(s => s.name === currentSubEntityName)) {
                setContextMode('entity', '');
            } else if (contextMode === 'entity') {
                // Re-apply so the entity path label / canvas reflect the new agenda.
                applyContextMode();
                syncContextTrigger();
            }
            // Existing behavior: auto-select the matching AI config for this entity.
            // When already in entity mode, history (loaded by applyContextMode) takes priority;
            // only use the static entity-config as a fallback when in task/none mode.
            if (entityPath && contextMode !== 'entity') {
                try {
                    const response = await fetch(`/api/ai/entity-config?path=${encodeURIComponent(entityPath)}`);
                    if (response.ok) {
                        const data = await response.json();
                        if (data.label) setDefaultAiConfig(data.label);
                    }
                } catch (error) {
                    console.error('Error fetching entity AI config:', error);
                }
            }
        });

        document.addEventListener('ai-task-selected', async (e) => {
            const detail = e.detail || {};
            const { taskId, entityPath, taskType, eventData } = detail;
            if (!taskId) return;

            // Remember this selection so re-selecting "Task" in the dropdown
            // can restore the editor + history.
            cachedTaskSelection = { taskId, entityPath, taskType, eventData };

            // Selecting a task implicitly switches the Context dropdown back to "Task".
            if (contextMode !== 'task') {
                contextMode = 'task';
                currentSubEntityName = '';
                syncContextTrigger();
            }

            // If already on this task, don't clear/reload (history & editor are already showing it).
            if (taskId === currentTaskId) return;

            currentTaskId   = taskId   || null;
            currentTaskPath = entityPath || '';

            // Clear prompt on task change
            if (aiPromptInput) aiPromptInput.value = '';

            if (!taskId) {
                setAiOutputPlaceholder();
                return;
            }

            await loadTaskHistory(taskId, entityPath);
        });

        document.addEventListener('ai-task-cleared', () => {
            currentTaskId   = null;
            currentTaskPath = '';
            lastUsedLabel   = null;
            cachedTaskSelection = null;
            setAiOutputPlaceholder();
        });
    }


    async function loadTaskHistory(taskId, entityPath) {
        try {
            const params = new URLSearchParams({ path: entityPath || '' });
            const resp = await fetch(`/api/project/${encodeURIComponent(taskId)}/history?${params}`);
            if (!resp.ok) { setAiOutputPlaceholder(); return; }
            const history = await resp.json();

            if (history && history.length > 0) {
                // Default to the last used AI config label (skip webhook callbacks)
                const lastWithLabel = [...history].reverse().find(m => m.label && !m.label.toLowerCase().includes('webhook'));
                if (lastWithLabel && lastWithLabel.label) {
                    lastUsedLabel = lastWithLabel.label;
                    setDefaultAiConfig(lastWithLabel.label);
                }
                // Show the last assistant response in the AI window
                const lastAssistant = [...history].reverse().find(m => m.role === 'assistant');
                if (lastAssistant) {
                    showResponseInOutput(lastAssistant.content, lastAssistant.thinking, 0,
                        lastAssistant.label, lastAssistant.timestamp);
                } else {
                    setAiOutputPlaceholder();
                }
            } else {
                setAiOutputPlaceholder();
            }
        } catch (e) {
            console.error('Error loading task history for AI window:', e);
            setAiOutputPlaceholder();
        }
    }

    /**
     * Render a full AI response in the AI output area.
     * No truncation — shows everything in a scrollable pre.
     */
    function showResponseInOutput(content, thinking, returncode, label, timestamp) {
        if (!aiOutput) return;

        let html = '';

        // Thinking block (if any and toggle is checked)
        if (thinking && aiShowThinking && aiShowThinking.checked) {
            html += `
                <details id="ai-thinking-section" class="ai-thinking-block" open>
                    <summary><i class="fas fa-brain"></i> Thinking</summary>
                    <pre class="ai-thinking-text">${escapeHtml(thinking)}</pre>
                </details>`;
        }

        if (returncode !== 0 && returncode != null) {
            html += `<div class="ai-error" style="margin-bottom:0.5rem"><i class="fas fa-exclamation-triangle"></i> Exited with code ${returncode}</div>`;
        }

        if (content) {
            html += `<pre class="ai-response-text">${escapeHtml(content.trim())}</pre>`;
        }

        if (label || timestamp) {
            const ts = timestamp ? new Date(timestamp).toLocaleString() : '';
            html += `<div class="ai-response-meta" style="font-size:0.76rem;color:#8899aa;margin-top:0.4rem">`;
            if (ts) html += `<span>${escapeHtml(ts)}</span>`;
            if (label) html += `${ts ? ' · ' : ''}<span>${escapeHtml(label)}</span>`;
            html += `</div>`;
        }

        aiOutput.innerHTML = html;
    }

    function setAiOutputPlaceholder() {
        if (aiOutput) aiOutput.innerHTML = '<p class="ai-placeholder">AI response will appear here...</p>';
    }

    /**
     * Fallback handler for canvas-stream-done CustomEvent.
     * Fires when canvas.js's _streamDoneCallback was null at 'done' time
     * (e.g. onerror cleared it before output events arrived).
     */
    function listenForStreamDone() {
        document.addEventListener('canvas-stream-done', function(e) {
            const { output, thinking, returncode, taskId, entityPath } = e.detail || {};
            console.log('[ai] canvas-stream-done fallback | outputLen=', (output||'').length,
                '| taskId=', taskId, '| hasPending=', !!_pendingStreamDone);
            if (_pendingStreamDone) {
                const cb = _pendingStreamDone;
                _pendingStreamDone = null;
                cb(output || '', thinking || '', returncode, taskId, entityPath);
            }
        });
    }

    // ─── Send AI Prompt ───────────────────────────────────────────────────────

    async function sendAiPrompt() {
        const prompt = aiPromptInput ? aiPromptInput.value.trim() : '';
        const selectedLabel = aiConfigSelect ? aiConfigSelect.value : '';

        if (!prompt) { showAiStatus('Please enter a prompt.', 'warning'); return; }
        if (!selectedLabel) { showAiStatus('Please select an AI configuration.', 'warning'); return; }
        if (isAiLoading) return;

        isAiLoading = true;
        if (aiSendBtn) {
            aiSendBtn.disabled = true;
            aiSendBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Sending...';
        }

        // Build full prompt prefix depending on the active Context mode.
        // - 'task'   : same as legacy behavior — include the selected task as context.
        // - 'entity' : no task context here; aicall.py will inject the entity's
        //              identity/omni/goals via the LOS_ENTITY_REL_PATH env var.
        // - 'none'   : strip all extra context — just the user's raw prompt.
        let fullPrompt = prompt;
        if (contextMode === 'task' && typeof window.getSelectedItemContext === 'function') {
            const ctx = window.getSelectedItemContext();
            if (ctx) {
                fullPrompt = `=== Selected Task or Item ===\n${ctx}\n=== End Selected Task or Item ===\n\n${prompt}`;
            }
        }


        lastUsedLabel = selectedLabel;
        const configType = getSelectedConfigType();

        // Save the user's prompt to history (only if a task is selected)
        if (currentTaskId) {
            await appendToHistory(currentTaskId, currentTaskPath, {
                role: 'user',
                label: selectedLabel,
                type: configType,
                content: prompt
            });
            // Immediately refresh canvas to show the pending block
            if (window.canvasAPI) window.canvasAPI.refreshAndExpandLast(currentTaskId);
        }

        // Show streaming indicator
        if (aiOutput) {
            aiOutput.innerHTML = `
                <div class="ai-loading">
                    <i class="fas fa-spinner fa-spin"></i> Waiting for response...
                </div>`;
        }

        try {
            // Forward the active Context mode + entity path so the server can
            // tell aicall.py to inject the entity's identity/omni/goals
            // (when mode == 'entity') and skip irrelevant context blocks.
            const streamBody = {
                label:        selectedLabel,
                prompt:       fullPrompt,
                context_mode: contextMode,
                entity_path:  (contextMode === 'entity') ? getActiveEntityPath() : (currentTaskPath || ''),
                project_id:   currentTaskId || null,
            };
            const streamResp = await fetch('/api/ai/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(streamBody)
            });


            if (!streamResp.ok) {
                const errData = await streamResp.json().catch(() => ({}));
                throw new Error(errData.error || `HTTP ${streamResp.status}`);
            }

            const { stream_id } = await streamResp.json();
            if (!stream_id) throw new Error('No stream_id returned from server');

            // Hand off to canvas.js to display the stream live.
            // The onDone callback fires directly when the stream completes (avoids CustomEvent timing).
            const streamDoneCallback = async (output, thinking, returncode, doneTaskId, donePath) => {
                // Clear the fallback reference immediately so listenForStreamDone's
                // canvas-stream-done handler cannot re-fire this same callback.
                _pendingStreamDone = null;

                isAiLoading = false;
                if (aiSendBtn) {
                    aiSendBtn.disabled = false;
                    aiSendBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Send';
                }

                // 1. Show full output in the AI window immediately
                showResponseInOutput(output || '', thinking || null, returncode, selectedLabel, new Date().toISOString());

                // 2. Save assistant response to history
                const saveTaskId = doneTaskId || currentTaskId;
                const savePath   = donePath   || currentTaskPath;
                console.log('[ai] streamDoneCallback | outputLen=', (output||'').length,
                    '| saveTaskId=', saveTaskId, '| doneTaskId=', doneTaskId,
                    '| currentTaskId=', currentTaskId, '| willSave=', !!(saveTaskId && output));
                if (saveTaskId && output) {
                    // Small delay: let the browser fully release the SSE connection before
                    // sending a new HTTP POST on the same connection pool.
                    await new Promise(resolve => setTimeout(resolve, 150));

                    // Don't save the raw thinking/debug buffer — it's aicall.py's verbose
                    // stderr debug output (17KB+). Only save it if it's a real model
                    // chain-of-thought (short, meaningful). Threshold: < 4KB.
                    const thinkingToSave = (thinking && thinking.length < 4096) ? thinking : null;
                    await appendToHistory(saveTaskId, savePath, {
                        role: 'assistant',
                        label: selectedLabel,
                        type: configType,
                        content: output.trim(),
                        thinking: thinkingToSave,
                        returncode: returncode
                    });
                }

                // 3. Refresh canvas — new pair will be expanded
                if (window.canvasAPI) window.canvasAPI.refreshAndExpandLast(saveTaskId);
            };

            // Store at module scope for CustomEvent fallback (if direct callback is lost)
            _pendingStreamDone = streamDoneCallback;

            if (window.canvasAPI) {
                window.canvasAPI.startStream(
                    stream_id,
                    selectedLabel,
                    currentTaskId,
                    currentTaskPath,
                    configType,
                    streamDoneCallback
                );
            }

            // Update AI output indicator
            if (aiOutput) {
                aiOutput.innerHTML = `
                    <div class="ai-loading">
                        <i class="fas fa-spinner fa-spin"></i> Waiting...
                    </div>`;
            }

        } catch (error) {
            console.error('AI stream error:', error);
            isAiLoading = false;
            if (aiSendBtn) {
                aiSendBtn.disabled = false;
                aiSendBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Send';
            }
            if (aiOutput) {
                aiOutput.innerHTML = `<div class="ai-error"><i class="fas fa-exclamation-triangle"></i> Error: ${escapeHtml(error.message)}</div>`;
            }
        }
    }

    // ─── History Management ───────────────────────────────────────────────────

    async function appendToHistory(taskId, entityPath, message) {
        if (!taskId) { console.warn('[history] skip: no taskId'); return; }
        console.log('[history] POSTing role=', message.role, 'to task=', taskId, 'contentLen=', (message.content||'').length);
        try {
            const params = new URLSearchParams({ path: entityPath || '' });
            const url = `/api/project/${encodeURIComponent(taskId)}/history?${params}`;
            const body = JSON.stringify(message);
            console.log('[history] fetch URL=', url, 'bodyLen=', body.length);
            const resp = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body
            });
            console.log('[history] fetch complete, status=', resp.status, 'role=', message.role);
            if (!resp.ok) {
                const errBody = await resp.json().catch(() => ({}));
                console.error(`[history] Save failed ${resp.status} for task=${taskId}:`, errBody);
            }
        } catch (e) {
            console.error('[history] Network error saving history:', e);
        }
    }

    // ─── Clear ────────────────────────────────────────────────────────────────

    function clearAiWindow() {
        if (aiPromptInput) aiPromptInput.value = '';
        setAiOutputPlaceholder();
        if (aiPromptInput) aiPromptInput.focus();
    }

    // ─── Helpers ──────────────────────────────────────────────────────────────

    function getSelectedConfigType() {
        if (!aiConfigSelect) return 'llm';
        const opt = aiConfigSelect.options[aiConfigSelect.selectedIndex];
        return opt?.dataset?.configType || 'llm';
    }

    function pollForAgentResponse(taskId, entityPath) {
        const interval = setInterval(async () => {
            if (!currentTaskId || currentTaskId !== taskId) {
                clearInterval(interval);
                return;
            }
            try {
                const resp = await fetch(`/api/project/${encodeURIComponent(taskId)}/agent-pending?path=${encodeURIComponent(entityPath)}`);
                const data = await resp.json();
                if (data.pending) {
                    clearInterval(interval);
                    showResponseInOutput(data.message, null, 0, 'Agent (Late Delivery)', new Date().toISOString());
                    if (window.canvasAPI) window.canvasAPI.refreshAndExpandLast(taskId);
                    isAiLoading = false;
                    if (aiSendBtn) {
                        aiSendBtn.disabled = false;
                        aiSendBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Send';
                    }
                }
            } catch (e) {
                console.error('Error polling for agent response:', e);
            }
        }, 5000); // Poll every 5 seconds
    }

    /**
     * Listen for agent-callback-received events dispatched by notifications.js
     * when an SSE notification of type 'agent_callback' arrives.
     * If the notification matches the currently open project, reload history
     * and refresh the canvas so the user sees the response immediately.
     */
    function listenForAgentCallback() {
        document.addEventListener('agent-callback-received', async (e) => {
            const { taskId, entityPath } = e.detail || {};
            if (!taskId || taskId !== currentTaskId) return;
            console.log('[ai] agent-callback-received for current task', taskId, entityPath);
            // Reload history to pick up the new callback response
            await loadTaskHistory(taskId, entityPath !== undefined ? entityPath : currentTaskPath);
            // Refresh canvas so the new entry is visible
            if (window.canvasAPI) window.canvasAPI.refreshAndExpandLast(taskId);
        });
    }

    function showAiStatus(message, type) {
        if (!aiOutput) return;
        const cls = type === 'warning' ? 'ai-warning' : 'ai-info';
        aiOutput.innerHTML = `<div class="${cls}"><i class="fas fa-info-circle"></i> ${escapeHtml(message)}</div>`;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }

    // ─── Expose for external use ──────────────────────────────────────────────
    window.setAiConfigLabel = setDefaultAiConfig;
});
