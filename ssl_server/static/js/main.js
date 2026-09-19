/**
 * Main JavaScript file for LOS Dashboard
 */

// DOM Elements
const resolutionDisplay = document.getElementById('resolution-display');
const themeSelect = document.getElementById('theme-select-dropdown');
const tabButtons = document.querySelectorAll('.tab-button');
const tabContents = document.querySelectorAll('.tab-content');
const exportDataBtn = document.getElementById('export-data-dropdown');
const importDataBtn = document.getElementById('import-data-dropdown');
const importFileInput = document.getElementById('import-file-dropdown');

// Utility Functions (formatDateTime kept for potential future use)
function formatDateTime(date) {
    return new Intl.DateTimeFormat('en-US', {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        hour: 'numeric',
        minute: 'numeric',
        second: 'numeric',
        hour12: true
    }).format(date);
}

// Update header date/time display
function initDateTime() {
    const dateTimeElement = document.getElementById('current-datetime');
    if (!dateTimeElement) return;
    
    function updateDateTime() {
        const now = new Date();
        dateTimeElement.textContent = formatDateTime(now);
    }
    
    // Update immediately and then every second
    updateDateTime();
    setInterval(updateDateTime, 1000);
}

// Detect and display resolution
function updateResolutionDisplay() {
    const width = window.innerWidth;
    const height = window.innerHeight;
    let resolutionType = '';
    
    if (width >= 3840) {
        resolutionType = '4K+';
    } else if (width >= 1920) {
        resolutionType = 'Full HD+';
    } else if (width >= 1280) {
        resolutionType = 'HD';
    } else {
        resolutionType = 'Standard';
    }
    
    resolutionDisplay.textContent = `Resolution: ${width}×${height} (${resolutionType})`;
    
    // Update layout based on resolution
    document.body.classList.toggle('high-resolution', width >= 3000);
}

// ─── All known theme CSS classes ─────────────────────────────────────────────
// Each non-light, non-system theme value maps to  "<value>-theme"  on <body>.
const ALL_THEME_CLASSES = [
    'dark-theme',
    'dark-green-theme',
    'dark-red-theme',
    'dark-white-theme',
    'midnight-blue-theme',
    'dracula-theme',
    'solarized-dark-theme',
    'monokai-theme',
    'nord-theme',
    'gruvbox-theme',
    'ocean-theme',
    'cyberpunk-theme',
    'forest-theme',
    'sunset-theme',
    'amber-terminal-theme',
    'high-contrast-theme',
];

// Theme switcher
function initTheme() {
    const savedTheme = localStorage.getItem('theme') || 'system';
    const validValues = Array.from(themeSelect.options).map(o => o.value);
    themeSelect.value = validValues.includes(savedTheme) ? savedTheme : 'system';
    applyTheme(themeSelect.value);

    themeSelect.addEventListener('change', () => {
        const theme = themeSelect.value;
        localStorage.setItem('theme', theme);
        applyTheme(theme);
    });
}

function applyTheme(theme) {
    // Strip every theme class first so they never stack
    document.body.classList.remove(...ALL_THEME_CLASSES);

    if (theme === 'system') {
        const prefersDark = window.matchMedia &&
                            window.matchMedia('(prefers-color-scheme: dark)').matches;
        if (prefersDark) document.body.classList.add('dark-theme');

        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
            if (themeSelect.value === 'system') {
                document.body.classList.remove(...ALL_THEME_CLASSES);
                if (e.matches) document.body.classList.add('dark-theme');
            }
        });
    } else if (theme !== 'light') {
        document.body.classList.add(theme + '-theme');
    }
    // light → no class (base body styles)

    updateResolutionDisplay();
}

// ─── Font family ─────────────────────────────────────────────────────────────

const FONT_FAMILIES = {
    'segoe':           "'Segoe UI', Tahoma, Geneva, Verdana, sans-serif",
    'system-ui':       "system-ui, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif",
    'inter':           "'Inter', sans-serif",
    'roboto':          "'Roboto', sans-serif",
    'open-sans':       "'Open Sans', sans-serif",
    'lato':            "'Lato', sans-serif",
    'nunito':          "'Nunito', sans-serif",
    'ubuntu':          "'Ubuntu', sans-serif",
    'source-code-pro': "'Source Code Pro', 'Courier New', monospace",
    'jetbrains-mono':  "'JetBrains Mono', 'Courier New', monospace",
    'georgia':         "Georgia, 'Times New Roman', serif",
    'merriweather':    "'Merriweather', Georgia, serif",
};

function initFont() {
    const fontSelect = document.getElementById('font-family-select');
    if (!fontSelect) return;

    const saved = localStorage.getItem('ui-font') || 'segoe';
    const validFonts = Object.keys(FONT_FAMILIES);
    fontSelect.value = validFonts.includes(saved) ? saved : 'segoe';
    applyFont(fontSelect.value);

    fontSelect.addEventListener('change', () => {
        localStorage.setItem('ui-font', fontSelect.value);
        applyFont(fontSelect.value);
    });
}

function applyFont(fontKey) {
    const stack = FONT_FAMILIES[fontKey] || FONT_FAMILIES['segoe'];
    document.documentElement.style.setProperty('--ui-font', stack);
}

// ─── Font size ────────────────────────────────────────────────────────────────

function initFontSize() {
    const sizeSelect = document.getElementById('font-size-select');
    if (!sizeSelect) return;

    const saved = localStorage.getItem('ui-font-size') || '14';
    // Validate against available options
    const validSizes = Array.from(sizeSelect.options).map(o => o.value);
    sizeSelect.value = validSizes.includes(saved) ? saved : '14';
    applyFontSize(sizeSelect.value);

    sizeSelect.addEventListener('change', () => {
        localStorage.setItem('ui-font-size', sizeSelect.value);
        applyFontSize(sizeSelect.value);
    });
}

function applyFontSize(px) {
    // Setting font-size on <html> scales all rem-based measurements
    document.documentElement.style.fontSize = px + 'px';
}

// ─── Canvas Focus Toggle ─────────────────────────────────────────────────────
// Toggles a body class that the CSS uses to either:
//   - <3000px: reveal the hidden canvas window and slide the main view off the left
//   - >=3000px: hide the normally-visible canvas so other windows expand
function initCanvasFocusToggle() {
    const btn = document.getElementById('canvas-toggle-btn');
    if (!btn) return;

    function updateIcon() {
        const active = document.body.classList.contains('canvas-focus-mode');
        btn.title = active
            ? 'Restore main view'
            : 'Show canvas / hide main view';
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }

    btn.addEventListener('click', () => {
        document.body.classList.toggle('canvas-focus-mode');
        updateIcon();
        // Let FullCalendar and other listeners know the layout changed
        window.dispatchEvent(new Event('resize'));
    });

    // Keep the icon in sync if the class is changed elsewhere
    updateIcon();
    new MutationObserver(updateIcon).observe(document.body, {
        attributes: true, attributeFilter: ['class']
    });
}

// Tab navigation
function initTabs() {
    tabButtons.forEach(button => {
        button.addEventListener('click', () => {
            const tabId = button.getAttribute('data-tab');

            // Update active tab button
            tabButtons.forEach(btn => btn.classList.remove('active'));
            button.classList.add('active');

            // Show active tab content
            tabContents.forEach(content => {
                content.classList.toggle('active', content.id === tabId);
            });

            // Refresh data when switching to specific tabs
            if (tabId === 'calendar-tab' && typeof window.reloadCalendarData === 'function') {
                window.reloadCalendarData();
            } else if (tabId === 'crontab-tab' && typeof window.loadCrontab === 'function') {
                window.loadCrontab();
            }
        });
    });

    // Global refresh button
    const globalRefreshBtn = document.getElementById('global-refresh');
    if (globalRefreshBtn) {
        globalRefreshBtn.addEventListener('click', () => {
            console.log('Global refresh button clicked');

            // Find the active tab and reload its data
            const activeTab = document.querySelector('.tab-button.active');
            if (activeTab) {
                const tabId = activeTab.getAttribute('data-tab');
                if (tabId === 'calendar-tab' && typeof window.reloadCalendarData === 'function') {
                    window.reloadCalendarData();
                } else if (tabId === 'todo-tab' && typeof window.reloadTodoData === 'function') {
                    window.reloadTodoData();
                } else if (tabId === 'crontab-tab' && typeof window.loadCrontab === 'function') {
                    window.loadCrontab();
                }
            }
        });
    }
}

// Data Export/Import
function initDataManagement() {
    // Export data
    exportDataBtn.addEventListener('click', async () => {
        try {
            const [calendarData, todoData, crontabData] = await Promise.all([
                fetch('/api/calendar').then(res => res.json()),
                fetch('/api/todos').then(res => res.json()),
                fetch('/api/crontab').then(res => res.json())
            ]);
            
            const exportData = {
                calendar: calendarData,
                todos: todoData,
                crontab: crontabData,
                exportDate: new Date().toISOString()
            };
            
            const dataStr = JSON.stringify(exportData, null, 2);
            const dataUri = 'data:application/json;charset=utf-8,'+ encodeURIComponent(dataStr);
            const exportFileDefaultName = `los_dashboard_export_${new Date().toISOString().slice(0,10)}.json`;
            
            const linkElement = document.createElement('a');
            linkElement.setAttribute('href', dataUri);
            linkElement.setAttribute('download', exportFileDefaultName);
            linkElement.click();
            
        } catch (error) {
            console.error('Export failed:', error);
            alert('Export failed. See console for details.');
        }
    });
    
    // Import data
    importDataBtn.addEventListener('click', () => {
        importFileInput.click();
    });
    
    importFileInput.addEventListener('change', async (event) => {
        try {
            const file = event.target.files[0];
            if (!file) return;
            
            const reader = new FileReader();
            reader.onload = async (e) => {
                try {
                    const data = JSON.parse(e.target.result);
                    
                    if (!confirm(`Import data from ${new Date(data.exportDate).toLocaleDateString()}? This will overwrite existing data.`)) {
                        return;
                    }
                    
                    if (data.calendar && Array.isArray(data.calendar)) {
                        const existingEvents = await fetch('/api/calendar').then(res => res.json());
                        for (const event of existingEvents) {
                            await fetch(`/api/calendar/${event.id}`, { method: 'DELETE' });
                        }
                        for (const event of data.calendar) {
                            await fetch('/api/calendar', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(event)
                            });
                        }
                    }
                    
                    if (data.todos && Array.isArray(data.todos)) {
                        const existingTodos = await fetch('/api/todos').then(res => res.json());
                        for (const todo of existingTodos) {
                            await fetch(`/api/todos/${todo.id}`, { method: 'DELETE' });
                        }
                        for (const todo of data.todos) {
                            await fetch('/api/todos', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(todo)
                            });
                        }
                    }
                    
                    if (data.crontab && Array.isArray(data.crontab)) {
                        const existingEntries = await fetch('/api/crontab').then(res => res.json());
                        for (const entry of existingEntries) {
                            await fetch(`/api/crontab/${entry.id}`, { method: 'DELETE' });
                        }
                        for (const entry of data.crontab) {
                            await fetch('/api/crontab', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(entry)
                            });
                        }
                    }
                    
                    alert('Import successful! Refreshing page to show imported data.');
                    window.location.reload();
                    
                } catch (error) {
                    console.error('Import processing failed:', error);
                    alert('Import failed. The file may be corrupted or in an invalid format.');
                }
            };
            reader.readAsText(file);
            
        } catch (error) {
            console.error('Import failed:', error);
            alert('Import failed. See console for details.');
        } finally {
            importFileInput.value = '';
        }
    });
}

// Initialize everything when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    updateResolutionDisplay();
    initTheme();
    initFont();
    initFontSize();
    initTabs();
    initCanvasFocusToggle();
    initDataManagement();
    initDateTime();
    
    window.addEventListener('resize', updateResolutionDisplay);
});
