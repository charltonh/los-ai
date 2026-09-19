/**
 * Todo List functionality for the Productivity Dashboard
 */

// DOM Elements
// Moved todoForm selection inside setupEventListeners
const todoInput = document.getElementById('new-todo');
const todoPriorityInput = document.getElementById('new-todo-priority'); // Added priority input
const todoList = document.getElementById('todo-list');
// Removed todoCount, clearCompletedBtn, filterButtons as they are no longer used

// State
let todos = [];
// Removed currentFilter
let currentTodoPath = ''; // Store the path for the current todo list view
let listenersAttached = false; // Flag to prevent multiple listener attachments

// Make todos accessible to other modules
window.todos = todos;

// Initialize Todo List
document.addEventListener('DOMContentLoaded', () => {
    console.log("DOMContentLoaded event fired."); // Log start of callback
    fetchTodos();
    console.log("Initial fetchTodos call potentially completed."); // Log after fetchTodos
    try {
        console.log("Attempting to call setupEventListeners..."); // Log before call
        setupEventListeners();
        console.log("setupEventListeners call completed."); // Log after call
    } catch (error) {
        console.error("Error occurred during setupEventListeners call:", error); // Log any error during the call
    }
    setupRefreshHandlers();
    window.reloadTodoData = reloadTodoData; // Expose reload function

    // Listen for todo updates from editor
    document.addEventListener('todo-updated', (e) => {
        if (e.detail && e.detail.id) {
            fetchTodos(); // Re-fetch to ensure local state is in sync with backend
        }
    });
});

function setupEventListeners() {
    if (listenersAttached) {
        console.log("setupEventListeners: Listeners already attached, skipping.");
        return; // Prevent re-attaching
    }
    console.log("Inside setupEventListeners function."); // Log entry
    try {
        const addTodoBtn = document.getElementById('add-todo-btn');
        if (addTodoBtn) {
            console.log("Found add-todo-btn inside setupEventListeners."); // Log finding button
            addTodoBtn.addEventListener('click', () => {
                console.log("Click listener attached INSIDE setupEventListeners fired!"); // Log click
                addTodo();
            });
            console.log("Listener attached INSIDE setupEventListeners."); // Log attachment
            listenersAttached = true; // Set the flag
        } else {
            console.error("setupEventListeners: Could not find add-todo-btn.");
        }
    } catch (error) {
        console.error("Error inside setupEventListeners:", error); // Catch errors within the function
    }
}

function setupRefreshHandlers() {
    const refreshTodoBtn = document.getElementById('refresh-todo');
    if (refreshTodoBtn) {
        refreshTodoBtn.addEventListener('click', () => {
            console.log('Manual refresh todo button clicked');
            reloadTodoData(currentTodoPath);
        });
    } else {
        console.warn('Refresh todo button not found');
    }
}

// API Functions
async function fetchTodos() {
    // Removed path parameter as backend uses global todo.txt now
    try {
        // Pass the current path as a query parameter
        const apiUrl = `/api/todos?path=${encodeURIComponent(currentTodoPath)}`;
        const response = await fetch(apiUrl);
        if (!response.ok) throw new Error('Failed to fetch todos');

        const receivedTodos = await response.json();
        console.log("DEBUG: fetchTodos received:", JSON.stringify(receivedTodos)); // Log received data
        todos = receivedTodos; // Assign to local scope variable
        window.todos = todos; // Update global reference
        console.log("DEBUG: fetchTodos assigned to window.todos:", JSON.stringify(window.todos)); // Log assigned data
        renderTodos();
    } catch (error) {
        console.error('Error fetching todos:', error);
        todoList.innerHTML = '<li>Error loading todos. Please try refreshing the page.</li>';
    }
}

async function addTodo() {
    console.log("Add button clicked."); // Log start
    const todoTextValue = todoInput.value.trim(); // Renamed variable for clarity
    const priorityValue = todoPriorityInput.value.trim();
    console.log("Text Input:", todoTextValue); // Log text input
    console.log("Priority Input:", priorityValue); // Log priority input

    if (!todoTextValue) {
        alert('Please enter the todo text.');
        todoInput.focus();
        return;
    }

    // Use default priority 1.0 if input is empty or invalid, backend handles clamping
    let priority = 1.0; // Default priority set to 1.0
    if (priorityValue !== '') {
        const parsedPriority = parseFloat(priorityValue);
        if (!isNaN(parsedPriority)) {
            priority = parsedPriority;
        } else {
            alert('Invalid priority value. Please enter a number between 0.0 and 9.9. Using 1.0.');
            // Keep default 1.0
        }
    }

    // Construct data object with 'text' key
    const newTodoData = {
        text: todoTextValue, // Use 'text' key
        priority: priority,
        path: currentTodoPath // Add the current path
        // Optional fields like status, assigned_to, deadline, delegatable_score
        // could be added here if there were inputs for them.
    };
    console.log("Data being sent:", newTodoData); // Log data object

    try {
        const apiUrl = `/api/todos`;
        console.log("Calling API:", apiUrl); // Log API call
        const response = await fetch(apiUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(newTodoData)
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ error: 'Failed to add todo' }));
            console.error("API Error Response:", errorData); // Log error response
            throw new Error(errorData.error || 'Failed to add todo');
        }

        // --- Handle successful response ---
        const addedTodo = await response.json(); // Backend now returns the created object
        console.log("API Success. Received:", addedTodo);

        // Add the new todo to the local state
        window.todos.push(addedTodo);
        // Re-sort the local array (optional, but good for consistency if order matters immediately)
        window.todos.sort((a, b) => (a.priority ?? 10.0) - (b.priority ?? 10.0));

        // Clear input fields
        todoInput.value = '';
        todoPriorityInput.value = ''; // Clear priority input too
        todoInput.focus();

        // Re-render the list with the new item included
        renderTodos();

    } catch (error) {
        console.error('Error adding todo:', error); // Log any other errors during fetch/processing
        alert(`Failed to add todo: ${error.message}`);
    }
}

// Removed toggleTodo function as completion status is no longer handled

// Update deleteTodo to accept only the ID
async function deleteTodo(id) {
    // Find the todo object in the current global 'todos' array
    const todoToDelete = window.todos.find(t => t.id === id);

    console.log(`deleteTodo(id=${id}): Found todo object:`, JSON.stringify(todoToDelete));

    if (!todoToDelete) {
        console.error(`deleteTodo: Could not find todo with ID ${id} in current list.`);
        alert("Error: Could not find the todo item to delete. Please refresh.");
        return;
    }

    // Use the 'text' field from the object for confirmation
    const textForConfirmation = todoToDelete.text; // Changed from .task
    const sourceFile = todoToDelete.source_file; // Get the source file

    if (!sourceFile) {
        console.error(`deleteTodo: Missing source_file for todo ID ${id}.`);
        alert("Error: Cannot delete item, source file information missing. Please refresh.");
        return;
    }

    // Show confirmation dialog using the 'text' field
    const confirmationMessage = `Are you sure you want to delete this task?\n\n"${textForConfirmation}"`;
    if (!confirm(confirmationMessage)) {
        return; // User cancelled
    }

    try {
        // Construct the new API URL with ID in path and source_file as query param
        const apiUrl = `/api/todos/${id}?source_file=${encodeURIComponent(sourceFile)}`;
        console.log(`Calling DELETE API: ${apiUrl}`); // Log the new URL

        const response = await fetch(apiUrl, {
            method: 'DELETE'
        });

        if (!response.ok) {
             const errorData = await response.json().catch(() => ({ error: 'Failed to delete todo' }));
             console.error("API Error Response (DELETE):", errorData);
             throw new Error(errorData.error || 'Failed to delete todo');
        }

        console.log("Delete successful via API.");

        // --- Handle successful response ---
        // Remove the todo from the local state array
        window.todos = window.todos.filter(todo => todo.id !== id);

        // Re-render the list from the modified local state
        renderTodos();

    } catch (error) {
        console.error('Error deleting todo:', error);
        alert(`Failed to delete todo: ${error.message}`); // Show specific error
    }
} // End of deleteTodo function

// Function to mark a todo item as done
async function markTodoAsDone(id) {
    console.log(`Attempting to mark todo ID ${id} as done.`);
    const todoToMark = window.todos.find(t => t.id === id);
    if (!todoToMark) {
        console.error(`markTodoAsDone: Could not find todo with ID ${id}.`);
        alert("Error: Could not find the todo item to mark as done.");
        return;
    }

    // If this todo is currently open in the editor with unsaved changes,
    // warn about them FIRST (before the Done confirmation), per UX expectation.
    const isLoadedInEditor = (window.currentEditorTaskId === id);
    if (isLoadedInEditor) {
        const saveBtn = document.getElementById('save-task');
        if (saveBtn && !saveBtn.disabled) {
            if (!confirm('You have unsaved changes to this task. Discard them and mark as done?')) {
                return; // User wants to go back and save first
            }
        }
    }

    if (!confirm(`Are you sure you want to mark the task "${todoToMark.text}" as done and move it?`)) {
        return; // User cancelled
    }

    try {
        // Ensure source_file is available (todoToMark is already defined and checked outside try)
        if (!todoToMark.source_file) {
            console.error(`markTodoAsDone: Missing source_file for todo ID ${id}.`);
            alert("Error: Cannot mark as done, source file information missing. Please refresh.");
            return;
        }

        const response = await fetch(`/api/todos/${id}/status`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            // Send source_file for backend context, as it's needed to locate the 'done.txt'
            body: JSON.stringify({ status: 'completed', source_file: todoToMark.source_file })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ error: 'Failed to mark as done' }));
            throw new Error(errorData.error || 'Failed to mark as done');
        }

        console.log(`Todo ID ${id} marked as done successfully via API.`);

        // Remove the todo from the local state array as it's now "done"
        window.todos = window.todos.filter(todo => todo.id !== id);
        renderTodos(); // Re-render the list, the item should be gone

        // If this todo was open in the editor, force-clear it without an
        // "unsaved changes" prompt — the action was already confirmed above.
        if (isLoadedInEditor && typeof window.clearEditorForced === 'function') {
            window.clearEditorForced();
        }

    } catch (error) {
        console.error('Error marking todo as done:', error);
        alert(`Failed to mark todo as done: ${error.message}`);
    }
}

// Removed clearCompleted function as completion status is no longer handled

// Render Functions
function renderTodos() {
    // Clear the list
    todoList.innerHTML = '';

    // Use window.todos directly for clarity
    const currentTodos = window.todos;

    if (!currentTodos || !Array.isArray(currentTodos) || currentTodos.length === 0) { // Added more robust check
        todoList.innerHTML = `<li class="empty-state">No tasks found. Add one above!</li>`;
        console.log("renderTodos: No todos to render or window.todos is not an array."); // Debug log
    } else {
        console.log(`renderTodos: Rendering ${currentTodos.length} todos.`); // Debug log
        // Add each todo to the list
        currentTodos.forEach((todo, index) => { // Added index for logging
            // *** Add Debugging Log Here ***
            console.log(`renderTodos loop index ${index}:`, JSON.stringify(todo));
            // Check types and specific properties
            console.log(`  >> typeof todo: ${typeof todo}`);
            if (typeof todo === 'object' && todo !== null) {
                console.log(`  >> todo.id: ${todo.id} (type: ${typeof todo.id})`);
                console.log(`  >> todo.text: ${todo.text} (type: ${typeof todo.text})`); // Check this value
                console.log(`  >> todo.priority: ${todo.priority} (type: ${typeof todo.priority})`); // Check this value
            } else {
                console.error(`  >> Item at index ${index} is not a valid object:`, todo);
            }
            // *** End Debugging Log ***

            try { // Add try-catch around element creation
                const todoItem = createTodoElement(todo);
                todoList.appendChild(todoItem);
            } catch (error) {
                console.error(`Error creating element for todo at index ${index}:`, todo, error);
                // Optionally add a placeholder LI indicating error
                const errorLi = document.createElement('li');
                errorLi.textContent = `Error rendering item ${index}`;
                errorLi.style.color = 'red';
                todoList.appendChild(errorLi);
            }
        });
    }
    // Removed counter update
}

// Helper function to highlight a todo item by ID
function highlightTodoById(id) {
    // Remove highlight from all todos
    document.querySelectorAll('.todo-item-highlighted').forEach(el => {
        el.classList.remove('todo-item-highlighted');
    });
    // Find and highlight the specific todo
    const todoEl = todoList.querySelector(`[data-id="${id}"]`);
    if (todoEl) {
        todoEl.classList.add('todo-item-highlighted');
    }
}

// Expose highlight function globally
window.highlightTodoById = highlightTodoById;

function createTodoElement(todo) {
    const li = document.createElement('li');
    // Removed completed class logic
    li.className = `todo-item`;
    li.dataset.id = todo.id; // Use the persistent ID from the backend

    // ── Drag handle (mousedown → DragTransfer) ────────────────────────────
    const dragHandle = document.createElement('span');
    dragHandle.className = 'todo-drag-handle';
    dragHandle.innerHTML = '<i class="fas fa-grip-vertical"></i>';
    dragHandle.title = 'Drag to move to a different entity';
    dragHandle.addEventListener('mousedown', (e) => {
        // Only respond to left-button
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        if (window.DragTransfer) {
            const todoEntityPath = (todo.entity_path !== undefined && todo.entity_path !== null)
                ? todo.entity_path : currentTodoPath;
            window.DragTransfer.start({
                type               : 'todo',
                id                 : todo.id,
                title              : todo.text || '(no text)',
                source_entity_path : todoEntityPath,
                source_file        : todo.source_file || null,
            }, e);
        }
    });
    li.appendChild(dragHandle);

    // Removed checkbox

    const todoText = document.createElement('span');
    todoText.className = 'todo-text';
    // Display priority (formatted to 2 decimal places) and the 'text' field
    // Use nullish coalescing for priority in case it's missing (though backend should add it)
    const displayPriority = (todo.priority ?? 1.0).toFixed(2);
    todoText.textContent = `[${displayPriority}] ${todo.text}`; // Use todo.text

    // Add click handler to open in editor
    todoText.addEventListener('click', (e) => {
        e.stopPropagation();
        highlightTodoById(todo.id);
        if (typeof loadTodoContent === 'function') {
            // Use the todo's own entity_path so notes/status are stored in the correct entity
            const todoEntityPath = (todo.entity_path !== undefined && todo.entity_path !== null)
                ? todo.entity_path
                : currentTodoPath;
            loadTodoContent(todo.id, todoEntityPath);
        }
    });

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'todo-delete';
    deleteBtn.innerHTML = '<i class="fas fa-trash"></i>';
    // Log the todo object just before attaching the listener
    console.log(`Attaching delete listener for ID ${todo.id} with text: "${todo.text}"`); // Use todo.text
    deleteBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        highlightTodoById(todo.id);
        // Pass only the ID to deleteTodo (deleteTodo retrieves the full object)
        deleteTodo(todo.id);
    });

    // Removed checkbox append
    li.appendChild(todoText);
    // li.appendChild(deleteBtn); // Replaced by buttonsDiv

    const markDoneBtn = document.createElement('button');
    markDoneBtn.className = 'todo-done-btn'; // Specific class for styling
    markDoneBtn.innerHTML = '<i class="fas fa-check"></i>';
    markDoneBtn.title = 'Mark as Done';
    markDoneBtn.addEventListener('click', (e) => {
        e.stopPropagation(); // Prevent li click event
        highlightTodoById(todo.id);
        markTodoAsDone(todo.id);
    });

    const buttonsDiv = document.createElement('div');
    buttonsDiv.className = 'todo-item-buttons'; // Container for styling buttons layout
    buttonsDiv.appendChild(deleteBtn); // Delete button first
    buttonsDiv.appendChild(markDoneBtn); // Then Mark Done button

    li.appendChild(buttonsDiv);

    // Make the whole item clickable (to open in editor)
    li.addEventListener('click', () => {
        highlightTodoById(todo.id);
        
        if (typeof loadTodoContent === 'function') {
            // Use the todo's own entity_path so notes/status are stored in the correct entity
            const todoEntityPath = (todo.entity_path !== undefined && todo.entity_path !== null)
                ? todo.entity_path
                : currentTodoPath;
            loadTodoContent(todo.id, todoEntityPath);
        }
    });

    return li;
}

// Function to reload todo data for a specific path
function reloadTodoData(entityPath = currentTodoPath) {
    console.log(`Reloading todo data for path: "${entityPath}"`);
    currentTodoPath = entityPath || ''; // Update the stored path
    fetchTodos(); // Refetch todos using the updated path
}

// Removed updateTodoCount function
// Removed filterTodos function
