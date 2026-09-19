// crontab.js - LOS Crontab UI

(function() {
    'use strict';

    let allEntries = [];
    let cronFiles = [];
    let activeFileFilter = 'all';
    let currentCronPath = '';  // Current entity path for scoping
    let editingEntry = null;   // Entry currently being edited, or null for add mode

    // --- DOM refs ---
    const typeSelect = document.getElementById('cron-type-select');
    const actionSelect = document.getElementById('cron-action-select');
    const fileSelect = document.getElementById('cron-file-select');
    const titleInput = document.getElementById('cron-title');
    const descInput = document.getElementById('cron-description');
    const commandInput = document.getElementById('cron-command');
    const addBtn = document.getElementById('add-cron-entry');
    const listEl = document.getElementById('crontab-list');
    const fileTabsEl = document.getElementById('cron-file-tabs');

    // Type-specific field containers
    const dateFields = document.getElementById('cron-date-fields');
    const cycleFields = document.getElementById('cron-cycle-fields');
    const exprFields = document.getElementById('cron-expr-fields');
    const commandRow = document.getElementById('cron-command-row');

    // Date fields
    const dateRuleSelect = document.getElementById('cron-date-rule');
    const monthField = document.getElementById('cron-month-field');
    const dayField = document.getElementById('cron-day-field');
    const nthField = document.getElementById('cron-nth-field');
    const weekdayField = document.getElementById('cron-weekday-field');
    const easterOffsetField = document.getElementById('cron-easter-offset-field');
    const monthSelect = document.getElementById('cron-month');
    const dayInput = document.getElementById('cron-day');
    const yearInput = document.getElementById('cron-year');
    const nthSelect = document.getElementById('cron-nth');
    const weekdaySelect = document.getElementById('cron-weekday');
    const easterOffsetInput = document.getElementById('cron-easter-offset');
    const originYearInput = document.getElementById('cron-origin-year');

    // Cycle fields
    const intervalDays = document.getElementById('cron-interval-days');
    const intervalHours = document.getElementById('cron-interval-hours');
    const intervalMinutes = document.getElementById('cron-interval-minutes');
    const anchorInput = document.getElementById('cron-anchor');

    // Cron expression fields
    const cronMin = document.getElementById('cron-min');
    const cronHour = document.getElementById('cron-hour');
    const cronDom = document.getElementById('cron-dom');
    const cronMon = document.getElementById('cron-mon');
    const cronDow = document.getElementById('cron-dow');

    const MONTH_NAMES = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    // Format a Date as YYYY-MM-DDTHH:MM in local time (for datetime-local inputs)
    function toLocalISOString(date) {
        const pad = (n) => String(n).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    // Track if this is the initial load
    let isInitialLoad = true;

    // --- Init ---
    function init() {
        if (!typeSelect) return; // Not on the right page

        // Set default anchor to now (local time, not UTC)
        if (anchorInput) {
            anchorInput.value = toLocalISOString(new Date());
        }

        typeSelect.addEventListener('change', onTypeChange);
        actionSelect.addEventListener('change', onActionChange);
        if (dateRuleSelect) dateRuleSelect.addEventListener('change', onDateRuleChange);
        addBtn.addEventListener('click', saveEntry);

        onTypeChange();
        onActionChange();
        onDateRuleChange();
        
        // Mark initial load as complete after first render
        isInitialLoad = false;
    }

    // Map entry types to their preferred default file
    const TYPE_DEFAULT_FILES = {
        'date': 'cron',
        'cycle': 'cycles',
        'cron': 'cron'
    };

    function onTypeChange() {
        const type = typeSelect.value;
        dateFields.style.display = type === 'date' ? '' : 'none';
        cycleFields.style.display = type === 'cycle' ? '' : 'none';
        exprFields.style.display = type === 'cron' ? '' : 'none';

        // Only auto-select the preferred file on initial load, not when user changes type
        if (isInitialLoad && fileSelect) {
            const preferredFile = TYPE_DEFAULT_FILES[type] || 'cron';
            // Check if the preferred file exists in the dropdown
            const options = Array.from(fileSelect.options);
            const match = options.find(o => o.value === preferredFile);
            if (match) {
                fileSelect.value = preferredFile;
            }
        }
    }

    function onActionChange() {
        commandRow.style.display = actionSelect.value === 'execute' ? '' : 'none';
    }

    function onDateRuleChange() {
        if (!dateRuleSelect) return;
        const rule = dateRuleSelect.value;
        // Fixed date: show month + day, hide nth/weekday/easter
        // nth_weekday: show month + nth + weekday, hide day/easter
        // last_weekday: show month + weekday, hide day/nth/easter
        // easter: show easter offset, hide month/day/nth/weekday
        monthField.style.display  = (rule === '' || rule === 'nth_weekday' || rule === 'last_weekday') ? '' : 'none';
        dayField.style.display    = (rule === '') ? '' : 'none';
        nthField.style.display    = (rule === 'nth_weekday') ? '' : 'none';
        weekdayField.style.display = (rule === 'nth_weekday' || rule === 'last_weekday') ? '' : 'none';
        easterOffsetField.style.display = (rule === 'easter') ? '' : 'none';
    }

    // --- Get current entity path ---
    function getEntityPath() {
        return currentCronPath;
    }

    // --- Load data ---
    function loadCrontab() {
        const path = getEntityPath();
        const url = `/api/crontab?path=${encodeURIComponent(path)}`;
        
        fetch(url)
            .then(r => r.json())
            .then(entries => {
                allEntries = entries;
                window.crontabEntries = allEntries; // Expose for AI context and other modules
                renderEntries();
            })
            .catch(err => console.error('Error loading crontab:', err));

        loadCronFiles();
    }

    function loadCronFiles() {
        const path = getEntityPath();
        fetch(`/api/crontab/files?path=${encodeURIComponent(path)}`)
            .then(r => r.json())
            .then(files => {
                cronFiles = files;
                renderFileTabs();
                updateFileSelect();
                // After dropdown is populated, auto-select the right file for current type
                onTypeChange();
            })
            .catch(err => console.error('Error loading cron files:', err));
    }

    // --- Render file tabs ---
    function renderFileTabs() {
        fileTabsEl.innerHTML = '';

        // "All" tab
        const allTab = document.createElement('button');
        allTab.className = 'cron-file-tab' + (activeFileFilter === 'all' ? ' active' : '');
        allTab.dataset.file = 'all';
        allTab.textContent = `All (${allEntries.length})`;
        allTab.addEventListener('click', () => { activeFileFilter = 'all'; renderFileTabs(); renderEntries(); });
        fileTabsEl.appendChild(allTab);

        // Per-file tabs
        const fileGroups = {};
        allEntries.forEach(e => {
            const fn = e.source_filename || 'unknown';
            fileGroups[fn] = (fileGroups[fn] || 0) + 1;
        });

        // Also add files that exist but might be empty
        cronFiles.forEach(f => {
            if (!fileGroups[f.name]) fileGroups[f.name] = f.count || 0;
        });

        Object.keys(fileGroups).sort().forEach(fn => {
            const tab = document.createElement('button');
            tab.className = 'cron-file-tab' + (activeFileFilter === fn ? ' active' : '');
            tab.dataset.file = fn;
            tab.textContent = `${fn} (${fileGroups[fn]})`;
            tab.addEventListener('click', () => { activeFileFilter = fn; renderFileTabs(); renderEntries(); });
            fileTabsEl.appendChild(tab);
        });
    }

    // --- Update file select dropdown ---
    let fileSelectListenerAttached = false;

    function updateFileSelect() {
        fileSelect.innerHTML = '';
        const seen = new Set();

        // Add files from the API
        cronFiles.forEach(f => {
            const opt = document.createElement('option');
            opt.value = f.name;
            opt.textContent = f.name;
            fileSelect.appendChild(opt);
            seen.add(f.name);
        });

        // Ensure 'cron' is always an option
        if (!seen.has('cron')) {
            const opt = document.createElement('option');
            opt.value = 'cron';
            opt.textContent = 'cron';
            fileSelect.insertBefore(opt, fileSelect.firstChild);
        }

        // Add "new file..." option
        const newOpt = document.createElement('option');
        newOpt.value = '__new__';
        newOpt.textContent = '+ New file...';
        fileSelect.appendChild(newOpt);

        // Only attach the listener once
        if (!fileSelectListenerAttached) {
            fileSelectListenerAttached = true;
            fileSelect.addEventListener('change', function() {
                if (this.value === '__new__') {
                    const name = prompt('Enter new cron file name (letters, numbers, underscores):');
                    if (name && /^[a-zA-Z0-9_-]+$/.test(name)) {
                        const opt = document.createElement('option');
                        opt.value = name;
                        opt.textContent = name;
                        fileSelect.insertBefore(opt, fileSelect.lastChild);
                        fileSelect.value = name;
                    } else {
                        fileSelect.value = 'cron';
                        if (name) alert('Invalid filename. Use only letters, numbers, underscores, hyphens.');
                    }
                }
            });
        }
    }

    // --- Render entries ---
    function renderEntries() {
        listEl.innerHTML = '';

        let filtered = allEntries;
        if (activeFileFilter !== 'all') {
            filtered = allEntries.filter(e => e.source_filename === activeFileFilter);
        }

        if (filtered.length === 0) {
            listEl.innerHTML = '<li class="cron-empty">No entries found.</li>';
            return;
        }

        // Sort: dates by month/day, cycles by title, cron by expression
        filtered.sort((a, b) => {
            if (a.type !== b.type) return a.type.localeCompare(b.type);
            if (a.type === 'date') {
                if (a.month !== b.month) return (a.month || 0) - (b.month || 0);
                return (a.day || 0) - (b.day || 0);
            }
            return (a.title || '').localeCompare(b.title || '');
        });

        filtered.forEach(entry => {
            const li = createEntryElement(entry);
            listEl.appendChild(li);
        });
    }

    function createEntryElement(entry) {
        const li = document.createElement('li');
        li.className = 'cron-entry' + (entry.enabled === false ? ' cron-disabled' : '');
        li.dataset.id = entry.id;

        // Type badge
        const typeBadge = document.createElement('span');
        typeBadge.className = 'cron-badge cron-badge-' + (entry.type || 'unknown');
        const typeIcons = { date: '📅', cycle: '🔄', cron: '⏰' };
        typeBadge.textContent = typeIcons[entry.type] || '❓';
        typeBadge.title = entry.type || 'unknown';

        // Title
        const titleSpan = document.createElement('span');
        titleSpan.className = 'cron-entry-title';
        titleSpan.textContent = entry.title || 'Untitled';

        // Schedule info
        const schedSpan = document.createElement('span');
        schedSpan.className = 'cron-entry-schedule';
        schedSpan.textContent = formatSchedule(entry);

        // Source file
        const srcSpan = document.createElement('span');
        srcSpan.className = 'cron-entry-source';
        srcSpan.textContent = entry.source_filename || '';

        // Action badge
        const actionBadge = document.createElement('span');
        actionBadge.className = 'cron-badge cron-badge-action-' + (entry.action || 'display');
        const actionIcons = { display: '👁', execute: '⚡', notify: '🔔' };
        actionBadge.textContent = actionIcons[entry.action] || '';
        actionBadge.title = entry.action || 'display';

        // Toggle button
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'cron-btn cron-toggle-btn';
        toggleBtn.innerHTML = entry.enabled !== false ? '<i class="fas fa-toggle-on"></i>' : '<i class="fas fa-toggle-off"></i>';
        toggleBtn.title = entry.enabled !== false ? 'Disable' : 'Enable';
        toggleBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleEntry(entry); });

        // Delete button
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'cron-btn cron-delete-btn';
        deleteBtn.innerHTML = '<i class="fas fa-trash"></i>';
        deleteBtn.title = 'Delete';
        deleteBtn.addEventListener('click', (e) => { e.stopPropagation(); deleteEntry(entry); });

        // ── Drag handle (mousedown → DragTransfer) ─────────────────────────
        const dragHandle = document.createElement('span');
        dragHandle.className = 'todo-drag-handle';
        dragHandle.innerHTML = '<i class="fas fa-grip-vertical"></i>';
        dragHandle.title = 'Drag to move to a different entity';
        dragHandle.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            e.preventDefault();
            e.stopPropagation();
            if (window.DragTransfer) {
                window.DragTransfer.start({
                    type               : 'cron',
                    id                 : entry.id,
                    title              : entry.title || '(untitled)',
                    source_entity_path : currentCronPath,
                    source_file        : entry.source_file || null,
                }, e);
            }
        });

        // Click on the row to edit
        li.style.cursor = 'pointer';
        li.addEventListener('click', () => startEditEntry(entry));

        // Assemble
        li.appendChild(dragHandle);
        li.appendChild(typeBadge);
        li.appendChild(titleSpan);
        li.appendChild(schedSpan);
        li.appendChild(srcSpan);
        li.appendChild(actionBadge);
        li.appendChild(toggleBtn);
        li.appendChild(deleteBtn);

        return li;
    }

    const WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const NTH_NAMES = ['', '1st', '2nd', '3rd', '4th', '5th'];

    function formatSchedule(entry) {
        if (entry.type === 'date') {
            const y = entry.year ? ` ${entry.year}` : ' (yearly)';
            const originSuffix = entry.origin_year ? ` [since ${entry.origin_year}]` : '';
            const rule = entry.rule;
            if (rule === 'easter') {
                const offset = entry.offset || 0;
                if (offset === 0) return `Easter Sunday${y}${originSuffix}`;
                return `Easter ${offset > 0 ? '+' : ''}${offset}d${y}${originSuffix}`;
            }
            if (rule === 'nth_weekday') {
                const nth = NTH_NAMES[entry.nth] || `${entry.nth}th`;
                const wd = WEEKDAY_NAMES[entry.weekday] || '?';
                const m = MONTH_NAMES[entry.month] || '?';
                return `${nth} ${wd} of ${m}${y}${originSuffix}`;
            }
            if (rule === 'last_weekday') {
                const wd = WEEKDAY_NAMES[entry.weekday] || '?';
                const m = MONTH_NAMES[entry.month] || '?';
                return `Last ${wd} of ${m}${y}${originSuffix}`;
            }
            // Fixed date
            const m = MONTH_NAMES[entry.month] || '?';
            const d = entry.day || '?';
            return `${m} ${d}${y}${originSuffix}`;
        }
        if (entry.type === 'cycle') {
            const i = entry.interval || {};
            const parts = [];
            if (i.days) parts.push(`${i.days}d`);
            if (i.hours) parts.push(`${i.hours}h`);
            if (i.minutes) parts.push(`${i.minutes}m`);
            return 'Every ' + (parts.join(' ') || '?');
        }
        if (entry.type === 'cron') {
            return entry.expression || formatCronSchedule(entry.schedule) || '?';
        }
        return '';
    }

    function formatCronSchedule(schedule) {
        if (!schedule) return '';
        return `${schedule.minute || '*'} ${schedule.hour || '*'} ${schedule.dayOfMonth || '*'} ${schedule.month || '*'} ${schedule.dayOfWeek || '*'}`;
    }

    // --- Build entry object from form ---
    function buildEntryFromForm() {
        const type = typeSelect.value;
        const title = titleInput.value.trim();
        if (!title) { alert('Title is required'); return null; }

        const entry = {
            type: type,
            title: title,
            description: descInput.value.trim(),
            action: actionSelect.value,
            enabled: true,
            command: actionSelect.value === 'execute' ? commandInput.value.trim() : null,
        };

        if (type === 'date') {
            const rule = dateRuleSelect ? dateRuleSelect.value : '';
            entry.rule = rule || null;
            entry.year = yearInput.value ? parseInt(yearInput.value) : null;
            entry.origin_year = originYearInput && originYearInput.value ? parseInt(originYearInput.value) : null;

            if (rule === 'easter') {
                entry.offset = parseInt(easterOffsetInput.value) || 0;
            } else if (rule === 'nth_weekday') {
                entry.month = parseInt(monthSelect.value);
                entry.nth = parseInt(nthSelect.value);
                entry.weekday = parseInt(weekdaySelect.value);
            } else if (rule === 'last_weekday') {
                entry.month = parseInt(monthSelect.value);
                entry.weekday = parseInt(weekdaySelect.value);
            } else {
                // Fixed date
                entry.month = parseInt(monthSelect.value);
                entry.day = parseInt(dayInput.value);
            }
        } else if (type === 'cycle') {
            entry.interval = {
                days: parseInt(intervalDays.value) || 0,
                hours: parseInt(intervalHours.value) || 0,
                minutes: parseInt(intervalMinutes.value) || 0
            };
            // Keep the datetime-local value as a naive local ISO string; do not
            // convert to UTC, otherwise the date shifts for negative UTC offsets.
            entry.anchor = anchorInput.value ? anchorInput.value + ':00' : toLocalISOString(new Date()) + ':00';
        } else if (type === 'cron') {
            entry.expression = `${cronMin.value} ${cronHour.value} ${cronDom.value} ${cronMon.value} ${cronDow.value}`;
        }

        return entry;
    }

    // --- Load entry into form for editing ---
    function startEditEntry(entry) {
        editingEntry = entry;

        // Switch type
        typeSelect.value = entry.type;
        onTypeChange();

        // Common fields
        titleInput.value = entry.title || '';
        descInput.value = entry.description || '';
        actionSelect.value = entry.action || 'display';
        onActionChange();
        if (entry.action === 'execute' && commandInput) commandInput.value = entry.command || '';

        if (entry.type === 'date') {
            const rule = entry.rule || '';
            if (dateRuleSelect) { dateRuleSelect.value = rule; onDateRuleChange(); }
            if (yearInput) yearInput.value = entry.year || '';
            if (originYearInput) originYearInput.value = entry.origin_year || '';

            if (rule === 'easter') {
                if (easterOffsetInput) easterOffsetInput.value = entry.offset || 0;
            } else if (rule === 'nth_weekday') {
                if (monthSelect) monthSelect.value = entry.month || 1;
                if (nthSelect) nthSelect.value = entry.nth || 1;
                if (weekdaySelect) weekdaySelect.value = entry.weekday != null ? entry.weekday : 0;
            } else if (rule === 'last_weekday') {
                if (monthSelect) monthSelect.value = entry.month || 1;
                if (weekdaySelect) weekdaySelect.value = entry.weekday != null ? entry.weekday : 0;
            } else {
                if (monthSelect) monthSelect.value = entry.month || 1;
                if (dayInput) dayInput.value = entry.day || '';
            }
        } else if (entry.type === 'cycle') {
            const iv = entry.interval || {};
            if (intervalDays) intervalDays.value = iv.days || 0;
            if (intervalHours) intervalHours.value = iv.hours || 0;
            if (intervalMinutes) intervalMinutes.value = iv.minutes || 0;
            if (anchorInput && entry.anchor) {
                // Convert ISO to datetime-local (strip seconds/Z)
                anchorInput.value = entry.anchor.slice(0, 16);
            }
        } else if (entry.type === 'cron') {
            const parts = (entry.expression || '* * * * *').split(' ');
            if (cronMin) cronMin.value = parts[0] || '*';
            if (cronHour) cronHour.value = parts[1] || '*';
            if (cronDom) cronDom.value = parts[2] || '*';
            if (cronMon) cronMon.value = parts[3] || '*';
            if (cronDow) cronDow.value = parts[4] || '*';
        }

        // File select - set to source file
        if (fileSelect && entry.source_filename) {
            fileSelect.value = entry.source_filename;
        }

        // Update button states
        addBtn.innerHTML = '<i class="fas fa-save"></i> Update Entry';
        addBtn.classList.add('cron-edit-mode');

        // Show cancel button
        let cancelBtn = document.getElementById('cancel-cron-edit');
        if (!cancelBtn) {
            cancelBtn = document.createElement('button');
            cancelBtn.id = 'cancel-cron-edit';
            cancelBtn.className = 'add-btn cron-cancel-btn';
            cancelBtn.innerHTML = '<i class="fas fa-times"></i> Cancel';
            cancelBtn.style.marginLeft = '8px';
            cancelBtn.addEventListener('click', cancelEditEntry);
            addBtn.parentNode.insertBefore(cancelBtn, addBtn.nextSibling);
        }
        cancelBtn.style.display = '';

        // Highlight the entry in the list
        highlightCrontabById(entry.id);

        // Scroll form into view
        addBtn.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // --- Cancel edit mode ---
    function cancelEditEntry() {
        editingEntry = null;
        addBtn.innerHTML = '<i class="fas fa-plus"></i> Add Entry';
        addBtn.classList.remove('cron-edit-mode');

        const cancelBtn = document.getElementById('cancel-cron-edit');
        if (cancelBtn) cancelBtn.style.display = 'none';

        // Clear highlighted entry
        listEl.querySelectorAll('.cron-entry').forEach(el => el.classList.remove('cron-entry-highlighted'));

        // Reset form
        titleInput.value = '';
        descInput.value = '';
        if (commandInput) commandInput.value = '';
        if (originYearInput) originYearInput.value = '';
        if (anchorInput) {
            anchorInput.value = toLocalISOString(new Date());
        }
    }

    // --- Save entry (add or update) ---
    function saveEntry() {
        const entry = buildEntryFromForm();
        if (!entry) return;

        if (editingEntry) {
            // UPDATE mode
            entry.id = editingEntry.id;
            entry.enabled = editingEntry.enabled !== false;

            const path = getEntityPath();
            const url = `/api/crontab/${encodeURIComponent(editingEntry.id)}?path=${encodeURIComponent(path)}`;

            fetch(url, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(entry)
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    cancelEditEntry();
                    loadCrontab();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => { console.error('Error updating entry:', err); alert('Failed to update entry'); });
        } else {
            // ADD mode
            const targetFile = fileSelect.value === '__new__' ? 'cron' : fileSelect.value;
            const path = getEntityPath();
            const url = `/api/crontab?path=${encodeURIComponent(path)}&file=${encodeURIComponent(targetFile)}`;

            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(entry)
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    titleInput.value = '';
                    descInput.value = '';
                    if (commandInput) commandInput.value = '';
                    if (originYearInput) originYearInput.value = '';
                    loadCrontab();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => { console.error('Error adding entry:', err); alert('Failed to add entry'); });
        }
    }

    // --- Toggle entry ---
    function toggleEntry(entry) {
        const path = getEntityPath();
        fetch(`/api/crontab/toggle/${entry.id}?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: entry.enabled === false })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) loadCrontab();
            else alert('Error: ' + (data.error || 'Unknown error'));
        })
        .catch(err => console.error('Error toggling entry:', err));
    }

    // --- Delete entry ---
    function deleteEntry(entry) {
        if (!confirm(`Delete "${entry.title}"?`)) return;

        const path = getEntityPath();
        fetch(`/api/crontab/${entry.id}?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.success) loadCrontab();
            else alert('Error: ' + (data.error || 'Unknown error'));
        })
        .catch(err => console.error('Error deleting entry:', err));
    }

    // --- Highlight a crontab entry by ID ---
    function highlightCrontabById(id) {
        if (!listEl) return;
        listEl.querySelectorAll('.cron-entry').forEach(el => {
            el.classList.remove('cron-entry-highlighted');
        });
        const entryEl = listEl.querySelector(`[data-id="${id}"]`);
        if (entryEl) {
            entryEl.classList.add('cron-entry-highlighted');
            entryEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
    window.highlightCrontabById = highlightCrontabById;

    // --- Expose for main.js and agenda.js ---
    window.loadCrontab = loadCrontab;
    window.initCrontab = init;

    // Called by agenda.js when entity selection changes
    window.reloadCrontabData = function(path) {
        if (path !== undefined) {
            currentCronPath = path || '';
        }
        activeFileFilter = 'all';  // Reset filter when switching entities
        // Cancel any active edit when switching entities
        if (editingEntry) cancelEditEntry();
        loadCrontab();
    };

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
