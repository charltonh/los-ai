/**
 * Task Editor functionality for the Productivity Dashboard
 */

// DOM Elements - Editor Core
const taskEditor = document.getElementById('task-editor');
const taskNotes = document.getElementById('task-notes');
const saveTaskBtn = document.getElementById('save-task');
const clearEditorBtn = document.getElementById('clear-editor');
const selectedTaskName = document.getElementById('selected-task-name');
const selectedTaskDate = document.getElementById('selected-task-date');
const selectedTaskPathSpan = document.getElementById('selected-task-path'); // Added span for path display
const editorTitlePath = document.getElementById('editor-title-path'); // Center title-bar path indicator
const taskStatusSelect = document.getElementById('task-status');
const taskPriorityInput = document.getElementById('task-priority'); // Added priority input
const priorityFieldContainer = document.getElementById('priority-field-container'); // Container for priority field
const delegateTaskBtn = document.getElementById('delegate-task-btn');
const editorTaskControls = document.querySelector('.editor-task-controls'); // Container for status/delegate

// DOM Elements - Modals
const delegateModal = document.getElementById('delegate-modal');
const delegatePersonModal = document.getElementById('delegate-person-modal');
const delegateCompanyModal = document.getElementById('delegate-company-modal');
const delegateLlmModal = document.getElementById('delegate-llm-modal');
const delegateAgentModal = document.getElementById('delegate-agent-modal');
const delegateComputerModal = document.getElementById('delegate-computer-modal');

// DOM Elements - Modal Forms & Inputs (Example for Person)
const delegatePersonForm = document.getElementById('delegate-person-form');
const delegatePersonNameInput = document.getElementById('delegate-person-name');
const delegatePersonContactInput = document.getElementById('delegate-person-contact');
// ... (Add similar references for other modal forms/inputs as needed)
const delegateComputerScriptInput = document.getElementById('delegate-computer-script');


// State
let currentTaskType = null; // 'todo', 'crontab', or 'calendar'
let toastTimeout = null;
let currentTaskId = null;
let currentEntityPath = null; // Store the path of the loaded item's entity
// Track the currently viewed agenda entity path (updated when user navigates the sidebar)
let viewedEntityPath = '';

// Listen for entity navigation so we know the "current directory"
document.addEventListener('entity-selected', (e) => {
    viewedEntityPath = (e.detail && e.detail.path) ? e.detail.path : '';
});

// ─── Context-dropdown cooperation (ai.js) ─────────────────────────────────
// The AI window's Context dropdown can ask us to:
//  • silently wipe the editor (without telling the canvas to clear) — when
//    switching to 'Entity' mode (canvas is then loaded with entity history).
//  • restore a previously-selected task — when switching back to 'Task' mode.
//
// We avoid the usual `ai-task-cleared` / `canvas-task-cleared` dispatch in
// these handlers so the canvas + dropdown stay in sync without re-entering
// the same state-change logic.
document.addEventListener('clear-task-editor-only', () => {
    if (!taskEditor) return;
    taskEditor.value = '';
    if (taskNotes) taskNotes.value = '';
    currentTaskId   = null;
    currentTaskType = null;
    currentDelegation = null;
    currentEntityPath = null;
    originalTaskData = {
        content: null, title: null, status: null, delegation: null, entityPath: null,
        calendarEventStart: null, calendarEventEnd: null, calendarEventAllDay: null,
        calendarEventColor: null, calendarOriginalExtendedProps: {},
        notes: null
    };
    if (selectedTaskName) selectedTaskName.textContent = 'None';
    if (selectedTaskPathSpan) selectedTaskPathSpan.textContent = '';
    if (editorTitlePath) editorTitlePath.textContent = '';
    if (selectedTaskDate) selectedTaskDate.textContent = '-';
    if (taskStatusSelect) taskStatusSelect.value = 'new';
    if (delegateTaskBtn) {
        delegateTaskBtn.style.background = '';
        delegateTaskBtn.style.backgroundImage = '';
    }
    if (editorTaskControls) editorTaskControls.style.display = 'none';
    if (saveTaskBtn) saveTaskBtn.disabled = true;
});

document.addEventListener('restore-task-selection', (e) => {
    const detail = e.detail || {};
    const { taskId, entityPath, taskType, eventData } = detail;
    if (!taskId || !taskType) return;
    try {
        if (taskType === 'todo' && typeof loadTodoContent === 'function') {
            loadTodoContent(taskId, entityPath || '');
        } else if (taskType === 'calendar' && eventData && typeof loadCalendarEventForEditor === 'function') {
            loadCalendarEventForEditor(eventData);
        } else if (taskType === 'crontab' && typeof loadCrontabContent === 'function') {
            loadCrontabContent(taskId, detail.command || '');
        }
    } catch (err) {
        console.warn('[editor] restore-task-selection failed:', err);
    }
});


// --- Path Normalisation Helper ---
/**
 * Strip /los/{username}/ prefix and data/... suffixes so we get
 * a clean relative entity path like "misc/house" or "" for root.
 */
function normalizeEntityPath(raw) {
    if (!raw) return '';
    // Remove /los/<username>/ (or /los/<username>) prefix
    let p = raw.replace(/^\/los\/[^/]+\/?/, '');
    // Remove data/todo/todo, data/calendar/..., data/cron/... suffixes
    p = p.replace(/\/data\/todo\/.*$/, '');
    p = p.replace(/data\/todo\/.*$/, '');
    p = p.replace(/\/data\/calendar\/.*$/, '');
    p = p.replace(/\/data\/cron\/.*$/, '');
    p = p.replace(/\/data\/.*$/, '');
    return p.replace(/\/$/, ''); // strip trailing slash
}
let originalTaskData = { // Store original state for change detection
    content: null, // For todos: details, For crontab: command, For calendar: description
    title: null, // For calendar events
    status: null,
    delegation: null, // Todo-specific
    entityPath: null,
    // Calendar specific original data
    calendarEventStart: null,
    calendarEventEnd: null,
    calendarEventAllDay: null,
    calendarEventColor: null,
    calendarOriginalExtendedProps: {},
    notes: null
};
let currentDelegation = null; // Holds the pending delegation details before saving

// Initialize Editor
document.addEventListener('DOMContentLoaded', () => {
    setupEditorEventListeners(); // Use renamed function
});
function setupEditorEventListeners() { // Renamed function
    // Removed DEBUG logs checking elements

    if (!saveTaskBtn || !taskEditor || !taskStatusSelect || !clearEditorBtn) {
        console.error("One or more essential editor elements not found! Listeners cannot be attached.");
        return; // Stop if truly essential elements are missing
    }
    clearEditorBtn.addEventListener('click', clearEditor);

    // Save task
    saveTaskBtn.addEventListener('click', saveTaskContent);

    // Track changes - Editor Content
    taskEditor.addEventListener('input', checkForChanges);

    // Track changes - Notes
    if (taskNotes) {
        taskNotes.addEventListener('input', () => {
            checkForChanges();
            saveNotesDebounced();
        });
    }

    // Track changes - Status
    taskStatusSelect.addEventListener('change', () => {
        checkForChanges();
    });

    // Track changes - Priority
    if (taskPriorityInput) {
        taskPriorityInput.addEventListener('input', () => {
            checkForChanges();
        });
    }

    // Delegate Button (optional element)
    if (delegateTaskBtn) {
        delegateTaskBtn.addEventListener('click', () => {
            openDelegateModal();
        });
    }

    // --- Modal Event Listeners ---

    // Close buttons (generic)
    document.querySelectorAll('.modal .close-modal, .modal .cancel-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const modalId = btn.closest('.modal').id;
            closeModal(modalId);
        });
    });

    // Main Delegate Modal Options
    delegateModal.querySelectorAll('.delegate-option').forEach(option => {
        option.addEventListener('click', handleDelegateOptionClick);
    });

    // Secondary Modal Form Submissions
    delegatePersonForm.addEventListener('submit', handleDelegatePersonSubmit);
    delegateCompanyModal.querySelector('form').addEventListener('submit', handleDelegateCompanySubmit);
    delegateLlmModal.querySelector('form').addEventListener('submit', handleDelegateLlmSubmit);
    delegateAgentModal.querySelector('form').addEventListener('submit', handleDelegateAgentSubmit);
    delegateComputerModal.querySelector('form').addEventListener('submit', handleDelegateComputerSubmit);

    // Hide editor controls initially
    editorTaskControls.style.display = 'none';
}

// --- Modal Management ---
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        // Reset forms within the modal before showing (optional but good practice)
        const form = modal.querySelector('form');
        if (form) form.reset();
        // Special case for computer script default
        if (modalId === 'delegate-computer-modal') {
            delegateComputerScriptInput.value = '#!/bin/bash\n';
        }
        modal.style.display = 'flex'; // Use flex for centering
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.style.display = 'none';
    }
}

// --- Toast Feedback ---
function showToast(message) {
    let toast = document.getElementById('editor-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'editor-toast';
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    
    toast.textContent = message;
    toast.classList.add('show');
    
    if (toastTimeout) clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// --- Notes Persistence ---
async function saveNotes(suppressToast = true) {
    if (!currentTaskId || !currentTaskType) return;
    
    // Determine path for notes saving
    let path = currentEntityPath || '';
    
    try {
        const response = await fetch(`/api/notes/${currentTaskId}?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ notes: taskNotes.value })
        });
        
        if (!response.ok) {
            throw new Error('Failed to save notes');
        } else {
            console.log('Notes saved successfully');
            originalTaskData.notes = taskNotes.value;
            checkForChanges();
        }
    } catch (error) {
        console.error('Error saving notes:', error);
        throw error;
    }
}

const saveNotesDebounced = debounce(async () => {
    // Auto-save notes silently in the background.
    // We deliberately do NOT update originalTaskData.notes or call checkForChanges()
    // here, so the Save button stays enabled for the user to click and get a toast.
    if (!currentTaskId || !currentTaskType) return;
    const path = currentEntityPath || '';
    try {
        const response = await fetch(`/api/notes/${currentTaskId}?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes: taskNotes.value })
        });
        if (response.ok) {
            console.log('Notes auto-saved (background)');
        }
    } catch (e) {
        // Ignore background save errors
    }
}, 1000);

async function loadNotes(taskId, path) {
    try {
        const response = await fetch(`/api/notes/${taskId}?path=${encodeURIComponent(path)}`);
        if (response.ok) {
            const data = await response.json();
            taskNotes.value = data.notes || '';
        } else {
            taskNotes.value = '';
        }
    } catch (error) {
        console.error('Error loading notes:', error);
        taskNotes.value = '';
    }
    originalTaskData.notes = taskNotes.value;
    checkForChanges();
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// --- Change Detection ---
function checkForChanges() {
    let contentChanged = false;
    let statusChanged = false;
    let priorityChanged = false;
    let delegationChanged = false; // Specific to todos
    let notesChanged = false;

    // Check notes change
    notesChanged = taskNotes.value !== (originalTaskData.notes || '');

    if (currentTaskType === 'todo') {
        contentChanged = taskEditor.value !== originalTaskData.content;
        statusChanged = taskStatusSelect.value !== originalTaskData.status;
        priorityChanged = parseFloat(taskPriorityInput.value) !== originalTaskData.priority;
        delegationChanged = JSON.stringify(currentDelegation) !== JSON.stringify(originalTaskData.delegation);
        saveTaskBtn.disabled = !(contentChanged || statusChanged || priorityChanged || delegationChanged || notesChanged);
    } else if (currentTaskType === 'crontab') {
        contentChanged = taskEditor.value !== originalTaskData.content;
        saveTaskBtn.disabled = !(contentChanged || notesChanged);
    } else if (currentTaskType === 'calendar') {
        // For calendar, 'content' in originalTaskData stores the description.
        // Title is not directly editable in the main textarea for simplicity, but status is.
        const descriptionFromEditor = taskEditor.value; // Assuming editor now holds only description for calendar
        contentChanged = descriptionFromEditor !== originalTaskData.content;
        statusChanged = taskStatusSelect.value !== originalTaskData.status;
        saveTaskBtn.disabled = !(contentChanged || statusChanged || notesChanged);
    } else {
        saveTaskBtn.disabled = true; // No task loaded
    }
}


// API Functions - Todo specific
// Modify loadTodoContent to accept entityPath
function loadTodoContent(todoId, entityPath) {
    currentTaskType = 'todo';
    currentTaskId = todoId;
    window.currentEditorTaskId = todoId; // Expose for external modules (e.g. todo.js checkmark button)
    currentEntityPath = entityPath; // Store the entity path
    editorTaskControls.style.display = 'flex'; // Show todo controls

    // Show priority field for todos
    if (priorityFieldContainer) {
        priorityFieldContainer.style.display = 'flex';
    }

    const todo = window.todos ? window.todos.find(t => t.id === todoId) : null;

    if (!todo) {
        console.error("Todo not found for loading:", todoId);
        clearEditor(); // Clear editor if todo is invalid
        return;
    }

    // Set editor content
    const content = todo.details || todo.text || ''; // Prefer details if available
    taskEditor.value = content;

    // Load notes
    loadNotes(todoId, entityPath);

    // Set status
    taskStatusSelect.value = todo.status || 'new'; // Default to 'new' if no status

    // Set priority
    const priorityValue = todo.priority !== undefined && todo.priority !== null ? todo.priority : 1.0;
    taskPriorityInput.value = priorityValue.toFixed(1);

    // Set delegation state
    currentDelegation = todo.delegation ? JSON.parse(JSON.stringify(todo.delegation)) : null; // Deep copy

    // Store original data — priority must be rounded to match the 1-decimal input display,
    // otherwise checkForChanges() will always see a false difference (e.g. 1.05 vs 1.1).
    originalTaskData = {
        content: content,
        status: taskStatusSelect.value, // Correctly store initial status
        priority: parseFloat(priorityValue.toFixed(1)), // Round to match input display precision
        delegation: currentDelegation ? JSON.parse(JSON.stringify(currentDelegation)) : null, // Deep copy
        entityPath: currentEntityPath // Store original entity path
    };

    // Update info panel
    const taskNameDisplay = todo.text ? todo.text.substring(0, 76) + (todo.text.length > 76 ? '...' : '') : 'New Todo';
    selectedTaskName.textContent = taskNameDisplay;
    selectedTaskDate.textContent = todo.lastModified ? formatDate(new Date(todo.lastModified)) : (todo.timestamp ? formatDate(new Date(todo.timestamp)) : 'New');
    // Compute normalised relative entity path for this item
    const displayPath = normalizeEntityPath(currentEntityPath || todo.source_file || '');
    // Clear inline path span — path goes to title bar instead
    selectedTaskPathSpan.textContent = '';
    selectedTaskPathSpan.title = todo.source_file || currentEntityPath || '';
    // Show in the title bar only when the item is from a different entity than the current view
    if (editorTitlePath) {
        editorTitlePath.textContent = (displayPath && displayPath !== viewedEntityPath) ? displayPath : '';
    }

    // Update delegate button color
    updateDelegateButtonColor(todo.delegatable_score);

    // Disable save button initially
    saveTaskBtn.disabled = true;
    if (delegateTaskBtn) delegateTaskBtn.style.display = 'block'; // Show delegate button for todos

    // Notify AI window and Canvas about the selected task
    document.dispatchEvent(new CustomEvent('ai-task-selected', {
        detail: { taskId: todoId, entityPath: entityPath, taskType: 'todo' }
    }));
    document.dispatchEvent(new CustomEvent('canvas-task-selected', {
        detail: { taskId: todoId, entityPath: entityPath }
    }));
}

// --- Calendar Event Handling in Editor ---
function loadCalendarEventForEditor(eventData) {
    currentTaskType = 'calendar';
    currentTaskId = eventData.id;
    currentEntityPath = eventData.entityPath || ''; // Path from calendar.js
    editorTaskControls.style.display = 'flex'; // Show status dropdown
    if (delegateTaskBtn) delegateTaskBtn.style.display = 'none'; // Hide delegate button for calendar events

    // Hide priority field for calendar events
    if (priorityFieldContainer) {
        priorityFieldContainer.style.display = 'none';
    }

    const description = eventData.extendedProps?.description || '';
    const status = eventData.extendedProps?.status || 'tentative';

    // Set title and description combined for calendar events if you want both in the 4-row area
    const title = eventData.title || '';
    taskEditor.value = title + (description ? '\n' + description : '');

    // Load notes
    loadNotes(eventData.id, currentEntityPath);
    taskStatusSelect.value = status;

    // Populate status dropdown with calendar-specific options if different
    // For now, assume taskStatusSelect is general or updated in HTML
    // Example: updateStatusDropdownOptions(['tentative', 'confirmed', 'cancelled', 'completed']);

    originalTaskData = {
        // Store the full combined value to match taskEditor.value exactly, so
        // checkForChanges() correctly detects only real edits (not a spurious mismatch).
        content: title + (description ? '\n' + description : ''),
        title: eventData.title, // Store original title separately
        status: status,
        delegation: null, // Not applicable to calendar events
        entityPath: currentEntityPath,
        calendarEventStart: eventData.start,
        calendarEventEnd: eventData.end,
        calendarEventAllDay: eventData.allDay,
        calendarEventColor: eventData.backgroundColor,
        calendarOriginalExtendedProps: JSON.parse(JSON.stringify(eventData.extendedProps || {}))
    };
    currentDelegation = null; // Reset delegation

    selectedTaskName.textContent = eventData.title ? eventData.title.substring(0, 76) + (eventData.title.length > 76 ? '...' : '') : 'Calendar Event';
    selectedTaskDate.textContent = eventData.start ? formatDate(new Date(eventData.start)) : 'New';
    
    // Compute normalised relative entity path for this item
    const calDisplayPath = normalizeEntityPath(currentEntityPath || '');
    // Clear inline path span — path goes to title bar instead
    selectedTaskPathSpan.textContent = '';
    selectedTaskPathSpan.title = currentEntityPath || '';
    // Show in the title bar only when the item is from a different entity than the current view
    if (editorTitlePath) {
        editorTitlePath.textContent = (calDisplayPath && calDisplayPath !== viewedEntityPath) ? calDisplayPath : '';
    }

    saveTaskBtn.disabled = true;

    // Notify AI window and Canvas about the selected calendar event.
    // Pass the full eventData so ai.js can cache it for context-switch restore.
    document.dispatchEvent(new CustomEvent('ai-task-selected', {
        detail: {
            taskId: eventData.id,
            entityPath: currentEntityPath,
            taskType: 'calendar',
            eventData: eventData
        }
    }));
    document.dispatchEvent(new CustomEvent('canvas-task-selected', {
        detail: { taskId: eventData.id, entityPath: currentEntityPath }
    }));
}



async function saveTodoContent() {
    if (!currentTaskId || currentTaskType !== 'todo') return;

    try {
        await saveNotes(true);
    } catch (e) {
        alert(`Failed to save notes: ${e.message}`);
        return;
    }

    const oldStatus = originalTaskData.status;
    const newStatus = taskStatusSelect.value;

    // Find the original todo in the global list to update it directly
    // This assumes window.todos is the source of truth displayed in the UI
    const todoIndex = window.todos ? window.todos.findIndex(t => t.id === currentTaskId) : -1;

    if (todoIndex === -1) {
        console.error('Todo not found in global list:', currentTaskId);
        alert('Error: Could not find the task to save.');
        return;
    }

    // Create the updated task object
    const updatedTodoData = {
        ...window.todos[todoIndex], // Start with existing data
        text: taskEditor.value.split('\n')[0], // Use first line as text summary
        details: taskEditor.value,
        status: taskStatusSelect.value,
        priority: parseFloat(taskPriorityInput.value), // Include priority from input
        delegation: currentDelegation, // Use the state variable holding delegation info
        lastModified: new Date().toISOString(), // This will be overwritten by server, but good practice
        entity_path: currentEntityPath // Add the entity path to the payload
        // Keep other fields like id, timestamp, delegatable_score etc.
    };

    // --- Handle status change to "completed" ---
    if ((taskStatusSelect.value === 'completed' || taskStatusSelect.value === 'done') && window.todos[todoIndex].status !== taskStatusSelect.value) {
        const todoToComplete = window.todos[todoIndex];
        if (!confirm(`Are you sure you want to mark the task "${todoToComplete.text}" as done and move it?`)) {
            taskStatusSelect.value = originalTaskData.status; // Revert dropdown if cancelled
            checkForChanges(); // Re-evaluate save button state
            return; // User cancelled
        }
        try {
            // const todoToComplete = window.todos[todoIndex]; // Moved up for confirm dialog
            if (!todoToComplete.source_file) {
                alert("Error: Source file information is missing for this todo. Cannot mark as completed.");
                console.error("Missing source_file for todo ID:", currentTaskId);
                return;
            }

            const statusUpdateResponse = await fetch(`/api/todos/${currentTaskId}/status`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    status: 'completed', // Map both 'done' and 'completed' to 'completed' for the move logic
                    source_file: todoToComplete.source_file
                })
            });

            if (!statusUpdateResponse.ok) {
                const errorData = await statusUpdateResponse.json().catch(() => ({ error: 'Failed to mark as completed' }));
                throw new Error(errorData.error || 'Failed to mark as completed');
            }

            console.log(`Todo ID ${currentTaskId} marked as completed via editor.`);
            showToast('Task marked as completed');

            // Remove from local todos array in todo.js (via event or direct call if safe)
            // For now, let todo.js handle removal on 'todo-updated' or a new specific event.
            // window.todos = window.todos.filter(todo => todo.id !== currentTaskId);

            // Notify todo.js to reload/refresh
            document.dispatchEvent(new CustomEvent('todo-updated', { detail: { id: currentTaskId, status: 'completed' } }));

            // Use clearEditorForced() so we don't re-run checkForChanges() and
            // get a spurious "unsaved changes" prompt — the action is already confirmed.
            clearEditorForced();
            return; // Exit after handling completion

        } catch (error) {
            console.error('Error marking todo as completed via editor:', error);
            alert(`Failed to mark todo as completed: ${error.message}`);
            return; // Stop if completion fails
        }
    }

    // --- Handle other updates (non-completion or content/delegation changes) ---
    try {
        console.log("DEBUG: Saving todo (non-completion or other changes) with entity path:", currentEntityPath);
        const response = await fetch(`/api/todos/${currentTaskId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(updatedTodoData)
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            console.error('Failed to update todo:', response.status, errorData);
            throw new Error(`Failed to update todo. Server responded with ${response.status}. ${errorData.error || errorData.message || ''}`);
        }

        const savedTodo = await response.json();
        console.log("DEBUG: Successfully saved todo data (non-completion):", savedTodo);

        originalTaskData = {
            content: savedTodo.details || savedTodo.text || '',
            status: savedTodo.status,
            priority: savedTodo.priority !== undefined && savedTodo.priority !== null ? savedTodo.priority : parseFloat(taskPriorityInput.value),
            delegation: savedTodo.delegation ? JSON.parse(JSON.stringify(savedTodo.delegation)) : null,
            entityPath: currentEntityPath, // Persist entityPath
            notes: taskNotes.value
        };
        currentDelegation = originalTaskData.delegation ? JSON.parse(JSON.stringify(originalTaskData.delegation)) : null;

        saveTaskBtn.disabled = true;
        selectedTaskDate.textContent = formatDate(new Date(savedTodo.lastModified));
        updateDelegateButtonColor(savedTodo.delegatable_score);

        showToast('Task saved');

        document.dispatchEvent(new CustomEvent('todo-updated', { detail: { id: currentTaskId, updatedTodo: savedTodo } }));

    } catch (error) {
        console.error('Error in saveTodoContent (non-completion) fetch/processing:', error);
        alert(`Failed to save todo content: ${error.message}`);
    }
}

// API Functions - Crontab specific
function loadCrontabContent(entryId, command) {
    currentTaskType = 'crontab';
    currentTaskId = entryId;
    editorTaskControls.style.display = 'none'; // Hide todo controls

    // Set editor content
    taskEditor.value = command || '';
    originalTaskData.content = taskEditor.value; // Store original content
    originalTaskData.status = null; // Reset non-applicable fields
    originalTaskData.delegation = null;
    currentDelegation = null;
    originalTaskContent = taskEditor.value;
    
    // Update info panel
    selectedTaskName.textContent = command ? command.substring(0, 30) + (command.length > 30 ? '...' : '') : 'New Crontab Entry';
    
    // Get crontab date
    const entry = window.crontabEntries ? window.crontabEntries.find(e => e.id === entryId) : null;
    if (entry && entry.timestamp) {
        const date = new Date(entry.timestamp);
        selectedTaskDate.textContent = formatDate(date);
    } else {
        selectedTaskDate.textContent = 'New';
    }
    
    // Enable/disable save button
    checkForChanges(); // Check if content differs from original
}

async function saveCrontabContent() {
    if (!currentTaskId || currentTaskType !== 'crontab') return;

    try {
        await saveNotes(true);
    } catch (e) {
        alert(`Failed to save notes: ${e.message}`);
        return;
    }
    
    // Find the crontab entry
    const entry = window.crontabEntries ? window.crontabEntries.find(e => e.id === currentTaskId) : null;
    
    if (!entry) {
        console.error('Crontab entry not found:', currentTaskId);
        return;
    }
    
    // Update crontab entry content
    entry.command = taskEditor.value;
    // entry.details = taskEditor.value; // Crontab might not need 'details'
    entry.lastModified = new Date().toISOString();
    
    try {
        // Update crontab via API
        const response = await fetch(`/api/crontab/${currentTaskId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(entry)
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.message || 'Failed to update crontab entry');
        }
        
        // Update original content to reflect saved state
        originalTaskData.content = taskEditor.value; // Update original content state
        saveTaskBtn.disabled = true;

        // Update last modified date in UI
        selectedTaskDate.textContent = formatDate(new Date(entry.lastModified));
        
        showToast('Crontab entry saved');

        // Notify crontab module to refresh
        document.dispatchEvent(new CustomEvent('crontab-updated', { detail: { id: currentTaskId } }));
        
    } catch (error) {
        console.error('Error saving crontab content:', error);
        alert(`Failed to save crontab content: ${error.message}`);
    }
}

// Common Functions
function saveTaskContent() {
    if (currentTaskType === 'todo') {
        saveTodoContent();
    } else if (currentTaskType === 'crontab') {
        saveCrontabContent();
    } else if (currentTaskType === 'calendar') {
        saveCalendarEventContent(); // New function for calendar
    }
}

async function saveCalendarEventContent() {
    if (!currentTaskId || currentTaskType !== 'calendar') return;

    try {
        await saveNotes(true);
    } catch (e) {
        alert(`Failed to save notes: ${e.message}`);
        return;
    }

    const oldStatus = originalTaskData.status;
    const newStatus = taskStatusSelect.value;

    // Separate title and description if multiple lines exist
    const lines = taskEditor.value.split('\n');
    const newTitle = lines[0];
    const newDescription = lines.slice(1).join('\n');

    // Construct the event data to send
    // Preserve original start, end, color, and other extendedProps not directly edited here.
    const updatedEventData = {
        id: currentTaskId,
        title: newTitle, // Save updated title from first line
        start: originalTaskData.calendarEventStart,
        end: originalTaskData.calendarEventEnd,
        allDay: originalTaskData.calendarEventAllDay,
        backgroundColor: originalTaskData.calendarEventColor,
        borderColor: originalTaskData.calendarEventColor, // Usually same as background
        extendedProps: {
            ...originalTaskData.calendarOriginalExtendedProps, // Preserve other existing props
            description: newDescription,
            status: newStatus
        }
        // entityPath is not part of the event object itself, but used for the API URL
    };

    try {
        const apiUrl = `/api/calendar?path=${encodeURIComponent(currentEntityPath)}`;
        const response = await fetch(apiUrl, { // POST to /api/calendar handles updates by ID
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(updatedEventData)
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(`Failed to save calendar event. Server: ${response.status}. ${errorData.error || ''}`);
        }

        const savedEventRaw = await response.json();
        // Backend returns {success: true, event: {...}} or similar
        const savedEvent = savedEventRaw.event || savedEventRaw;

        // Update originalTaskData to reflect saved state
        const extendedProps = savedEvent.extendedProps || {};
        const savedTitle = savedEvent.title || '';
        const savedDescription = extendedProps.description || '';
        // Keep content as full combined value to match taskEditor.value format
        originalTaskData.content = savedTitle + (savedDescription ? '\n' + savedDescription : '');
        originalTaskData.status = extendedProps.status || 'tentative';
        // Update original title
        originalTaskData.title = savedEvent.title;
        originalTaskData.calendarOriginalExtendedProps = JSON.parse(JSON.stringify(extendedProps));


        saveTaskBtn.disabled = true;
        selectedTaskDate.textContent = formatDate(new Date(savedEvent.start)); // Or a lastModified if backend adds it

        showToast('Calendar event saved');

        // Notify calendar.js to refetch events
        document.dispatchEvent(new CustomEvent('calendar-updated', { detail: { id: currentTaskId, updatedEvent: savedEvent } }));
        // Also, calendar.js's own refetchEvents on the calendar instance might be sufficient if it's triggered by modal closure.

    } catch (error) {
        console.error('Error saving calendar event content:', error);
        alert(`Failed to save calendar event: ${error.message}`);
    }
}


function clearEditor() {
    // Re-evaluate change state before checking whether to show the dialog
    checkForChanges();
    if (!saveTaskBtn.disabled) { // If button is enabled, there are unsaved changes
        if (!confirm('You have unsaved changes. Are you sure you want to clear the editor?')) {
            return;
        }
    }
    _doClearEditor();
}

/**
 * Force-clear the editor without prompting for unsaved changes.
 * Use this after a confirmed action (e.g. marking done from the todo list)
 * that already handled or discarded any pending changes.
 */
function clearEditorForced() {
    _doClearEditor();
}
window.clearEditorForced = clearEditorForced;

/** Shared inner clear logic (no prompt). */
function _doClearEditor() {
    taskEditor.value = '';
    taskNotes.value = '';
    currentTaskId = null;
    window.currentEditorTaskId = null;
    currentTaskType = null;
    currentDelegation = null;
    currentEntityPath = null; 
    originalTaskData = { 
        content: null, title: null, status: null, delegation: null, entityPath: null,
        calendarEventStart: null, calendarEventEnd: null, calendarEventAllDay: null,
        calendarEventColor: null, calendarOriginalExtendedProps: {},
        notes: null
    };

    selectedTaskName.textContent = 'None';
    selectedTaskPathSpan.textContent = '';
    if (editorTitlePath) editorTitlePath.textContent = '';
    selectedTaskDate.textContent = '-';
    taskStatusSelect.value = 'new'; // Reset status dropdown
    if (delegateTaskBtn) {
        delegateTaskBtn.style.background = ''; // Reset delegate button color
        delegateTaskBtn.style.backgroundImage = '';
    }

    editorTaskControls.style.display = 'none'; // Hide todo controls
    saveTaskBtn.disabled = true;

    // Notify AI window and Canvas that no task is selected
    document.dispatchEvent(new CustomEvent('ai-task-cleared', {}));
    document.dispatchEvent(new CustomEvent('canvas-task-cleared', {}));
}

// --- Delegation Logic ---

function openDelegateModal() {
    if (!currentTaskId || currentTaskType !== 'todo') {
        return;
    }
    // Highlight 'Myself' by default or based on current delegation
    const myselfOption = delegateModal.querySelector('.delegate-option[data-delegate-type="myself"]');
    delegateModal.querySelectorAll('.delegate-option').forEach(opt => opt.classList.remove('selected')); // Clear previous selection visual

    if (!currentDelegation || currentDelegation.type === 'myself') {
         myselfOption.classList.add('selected'); // Example visual cue
         myselfOption.style.outline = '2px solid var(--accent-color)'; // Use existing CSS style
    } else {
         myselfOption.style.outline = 'none';
         // Optionally highlight the currently delegated type
         const currentOption = delegateModal.querySelector(`.delegate-option[data-delegate-type="${currentDelegation.type}"]`);
         if (currentOption) {
             currentOption.classList.add('selected');
             currentOption.style.outline = '2px solid var(--accent-color)';
         }
    }


    openModal('delegate-modal');
}

function handleDelegateOptionClick(event) {
    const delegateType = event.target.closest('.delegate-option').dataset.delegateType;
    closeModal('delegate-modal'); // Close main modal

    switch (delegateType) {
        case 'myself':
            currentDelegation = { type: 'myself' };
            taskStatusSelect.value = 'new'; // Or 'in_progress'? Revert status?
            checkForChanges();
            // Maybe add a visual confirmation?
            break;
        case 'person':
            openModal('delegate-person-modal');
            // Pre-fill form if editing existing delegation
            if (currentDelegation && currentDelegation.type === 'person') {
                delegatePersonNameInput.value = currentDelegation.name || '';
                delegatePersonContactInput.value = currentDelegation.contact || '';
            }
            break;
        case 'company':
             openModal('delegate-company-modal');
             if (currentDelegation && currentDelegation.type === 'company') {
                 delegateCompanyModal.querySelector('#delegate-company-name').value = currentDelegation.name || '';
                 delegateCompanyModal.querySelector('#delegate-company-contact').value = currentDelegation.contact || '';
             }
             break;
        case 'llm':
             openModal('delegate-llm-modal');
              if (currentDelegation && currentDelegation.type === 'llm') {
                 delegateLlmModal.querySelector('#delegate-llm-model').value = currentDelegation.model || '';
                 delegateLlmModal.querySelector('#delegate-llm-url').value = currentDelegation.url || '';
                 delegateLlmModal.querySelector('#delegate-llm-key').value = currentDelegation.apiKey || '';
             }
            break;
        case 'agent':
             openModal('delegate-agent-modal');
              if (currentDelegation && currentDelegation.type === 'agent') {
                 delegateAgentModal.querySelector('#delegate-agent-name').value = currentDelegation.name || '';
                 delegateAgentModal.querySelector('#delegate-agent-url').value = currentDelegation.url || '';
                 delegateAgentModal.querySelector('#delegate-agent-key').value = currentDelegation.apiKey || '';
             }
            break;
        case 'computer':
             openModal('delegate-computer-modal');
              if (currentDelegation && currentDelegation.type === 'computer') {
                 delegateComputerScriptInput.value = currentDelegation.script || '#!/bin/bash\n';
             }
            break;
        // Crew and Quantum are disabled, no action needed
        default:
            console.warn("Unknown delegation type:", delegateType);
    }
}

// Form Submission Handlers (Example for Person)
function handleDelegatePersonSubmit(event) {
    event.preventDefault();
    currentDelegation = {
        type: 'person',
        name: delegatePersonNameInput.value.trim(),
        contact: delegatePersonContactInput.value.trim() || null // Store as null if empty
    };
    taskStatusSelect.value = 'delegated'; // Set status to delegated
    closeModal('delegate-person-modal');
    checkForChanges(); // Check if this change enables save
}

function handleDelegateCompanySubmit(event) {
    event.preventDefault();
    currentDelegation = {
        type: 'company',
        name: delegateCompanyModal.querySelector('#delegate-company-name').value.trim(),
        contact: delegateCompanyModal.querySelector('#delegate-company-contact').value.trim() || null
    };
    taskStatusSelect.value = 'delegated';
    closeModal('delegate-company-modal');
    checkForChanges();
}

function handleDelegateLlmSubmit(event) {
    event.preventDefault();
    currentDelegation = {
        type: 'llm',
        model: delegateLlmModal.querySelector('#delegate-llm-model').value.trim(),
        url: delegateLlmModal.querySelector('#delegate-llm-url').value.trim() || null,
        apiKey: delegateLlmModal.querySelector('#delegate-llm-key').value.trim() || null // Be careful storing API keys client-side
    };
    taskStatusSelect.value = 'delegated';
    closeModal('delegate-llm-modal');
    checkForChanges();
}

function handleDelegateAgentSubmit(event) {
    event.preventDefault();
    currentDelegation = {
        type: 'agent',
        name: delegateAgentModal.querySelector('#delegate-agent-name').value.trim(),
        url: delegateAgentModal.querySelector('#delegate-agent-url').value.trim() || null,
        apiKey: delegateAgentModal.querySelector('#delegate-agent-key').value.trim() || null
    };
    taskStatusSelect.value = 'delegated';
    closeModal('delegate-agent-modal');
    checkForChanges();
}

function handleDelegateComputerSubmit(event) {
    event.preventDefault();
    currentDelegation = {
        type: 'computer',
        script: delegateComputerScriptInput.value // Store the full script
    };
    taskStatusSelect.value = 'delegated';
    closeModal('delegate-computer-modal');
    checkForChanges();
}


// --- Delegate Button Coloring ---
function updateDelegateButtonColor(score) {
    const button = delegateTaskBtn;
    if (!button) return; // Guard: element may not exist in current view
    button.style.background = ''; // Clear previous background
    button.style.backgroundImage = ''; // Clear previous gradient

    if (score === null || score === undefined) {
        button.style.backgroundColor = 'var(--primary-color)'; // Default blue
        return;
    }

    score = Math.max(0, Math.min(99.99, score)); // Clamp score

    // Define HSL color stops for the rainbow gradient + purple/red ends
    // Hues: Purple (270) -> Blue (240) -> Cyan (180) -> Green (120) -> Yellow (60) -> Orange (30) -> Red (0)
    let hue;
    if (score === 0.0) {
        hue = 270; // Dark Purple
    } else if (score >= 99.99) {
        hue = 0; // Bright Red
    } else {
        // Map 0-100 score range to 270 (Purple) -> 0 (Red) hue range
        // We want yellow (60) around score 50-60
        // Let's try a non-linear mapping or segments
        if (score < 50) {
            // Map 0-50 to Hue 270 -> 60 (Purple to Yellow)
             hue = 270 - (score / 50) * (270 - 60);
        } else {
             // Map 50-100 to Hue 60 -> 0 (Yellow to Red)
             hue = 60 - ((score - 50) / 50) * 60;
        }
    }

     // Adjust saturation and lightness for vibrancy
    const saturation = '100%';
    // Make purple/red darker/brighter?
    let lightness = '50%';
    if (score === 0.0) lightness = '35%'; // Darker Purple
    if (score >= 99.99) lightness = '55%'; // Brighter Red


    button.style.backgroundColor = `hsl(${hue}, ${saturation}, ${lightness})`;
}

function formatDate(date) {
    return new Intl.DateTimeFormat('en-US', {
        year: 'numeric', 
        month: 'short', 
        day: 'numeric',
        hour: 'numeric',
        minute: 'numeric'
    }).format(date);
}

/**
 * Returns a formatted string with all info about the currently selected Task Manager item
 * (todo, calendar event, or cron entry), including all JSON fields and notes.
 * Returns null if no item is selected.
 * Exposed globally so ai.js can inject it into the AI context window.
 */
window.getSelectedItemContext = function() {
    if (!currentTaskType || !currentTaskId) return null;

    const lines = [];

    if (currentTaskType === 'todo') {
        const todo = window.todos ? window.todos.find(t => t.id === currentTaskId) : null;
        if (!todo) return null;

        lines.push('Type: Todo');
        lines.push(`Title/Text: ${todo.text || ''}`);

        // Full JSON data (strip internal/file-system fields)
        const fields = Object.assign({}, todo);
        delete fields.source_file;
        lines.push(`Full Data:\n${JSON.stringify(fields, null, 2)}`);

    } else if (currentTaskType === 'calendar') {
        lines.push('Type: Calendar Event');
        lines.push(`Title: ${originalTaskData.title || ''}`);
        lines.push(`Start: ${originalTaskData.calendarEventStart || ''}`);
        if (originalTaskData.calendarEventEnd) {
            lines.push(`End: ${originalTaskData.calendarEventEnd}`);
        }
        lines.push(`All Day: ${originalTaskData.calendarEventAllDay ? 'Yes' : 'No'}`);
        lines.push(`Status: ${originalTaskData.status || ''}`);

        // Extended properties (description, delegatable_score, source, etc.)
        const extProps = originalTaskData.calendarOriginalExtendedProps || {};
        if (Object.keys(extProps).length > 0) {
            lines.push(`Extended Properties:\n${JSON.stringify(extProps, null, 2)}`);
        }

    } else if (currentTaskType === 'crontab') {
        const entries = window.crontabEntries || [];
        const entry = entries.find(e => e.id === currentTaskId);

        if (entry) {
            lines.push('Type: Cron Entry');
            lines.push(`Title: ${entry.title || ''}`);
            const fields = Object.assign({}, entry);
            delete fields.source_file;
            lines.push(`Full Data:\n${JSON.stringify(fields, null, 2)}`);
        } else {
            // Fallback: minimal info from editor state
            lines.push('Type: Cron Entry');
            lines.push(`ID: ${currentTaskId}`);
            const editorContent = taskEditor ? taskEditor.value : '';
            if (editorContent) lines.push(`Content: ${editorContent}`);
        }

    } else {
        return null;
    }

    // Entity path
    if (currentEntityPath) {
        lines.push(`Entity Path: ${currentEntityPath}`);
    }

    // Notes
    const notesContent = taskNotes ? taskNotes.value : '';
    if (notesContent && notesContent.trim()) {
        lines.push(`Notes:\n${notesContent.trim()}`);
    }

    return lines.join('\n');
};
