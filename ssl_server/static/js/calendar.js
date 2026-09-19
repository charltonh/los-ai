/**
 * Calendar functionality for the Productivity Dashboard
 */

// DOM Elements
const calendarEl = document.getElementById('calendar');
const eventModal = document.getElementById('event-modal');
const eventForm = document.getElementById('event-form');
const eventTitleInput = document.getElementById('event-title');
const eventStartInput = document.getElementById('event-start');
const eventEndInput = document.getElementById('event-end');
const eventColorInput = document.getElementById('event-color');
const eventDescriptionInput = document.getElementById('event-description');
const eventIdInput = document.getElementById('event-id');
const deleteEventBtn = document.getElementById('delete-event');
const closeModalBtns = document.querySelectorAll('.close-modal, .cancel-btn');

// Calendar instance
let calendar;
let currentCalendarPath = ''; // Store the path for the current calendar view

// State for double-click detection
let lastClickTime = 0;
let lastClickTarget = null; // Can be a date string or event ID
const DOUBLE_CLICK_THRESHOLD = 400; // milliseconds

// State for modal visibility tracking
let isEventModalOpen = false;

// Custom FullCalendar styles for event content wrapping
const calendarStyles = `
    .fc-event-main {
        white-space: normal !important;
        overflow: hidden !important;
        height: 100%;
    }
    .fc-v-event .fc-event-main {
        flex-grow: 1;
        flex-shrink: 1;
        min-height: 0;
    }
    .fc-event-content-wrapper {
        height: 100%;
    }
`;

// Initialize Calendar
document.addEventListener('DOMContentLoaded', () => {
    initCalendar();
    setupEventHandlers();
    setupRefreshHandlers();
    window.reloadCalendarData = reloadCalendarData; // Expose reload function

    // Listen for calendar updates from editor
    document.addEventListener('calendar-updated', (e) => {
        if (calendar) {
            calendar.refetchEvents();
        }
    });
});

function initCalendar() {
    // Inject custom styles
    const styleEl = document.createElement('style');
    styleEl.textContent = calendarStyles;
    document.head.appendChild(styleEl);

    calendar = new FullCalendar.Calendar(calendarEl, {
        initialView: 'dayGridMonth',
        headerToolbar: {
            left: 'prev,next today',
            center: 'title',
            right: 'dayGridMonth,timeGridWeek,timeGridDay'
        },
        height: '100%',
        selectable: true, // date range selection
        editable: true,  // drag-and-drop editing
        events: function(fetchInfo, successCallback, failureCallback) {
            // Use the stored path to fetch events
            const apiUrl = `/api/calendar?path=${encodeURIComponent(currentCalendarPath)}`;
            fetch(apiUrl)
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    successCallback(data);
                })
                .catch(error => {
                    console.error('Error fetching calendar events:', error);
                    failureCallback(error);
                });
        },
        eventTimeFormat: {
            hour: '2-digit',
            minute: '2-digit',
            hour12: true // Keep this for modal formatting if needed
        },
        eventDisplay: 'block', // Display events as blocks without time text
        
        dateClick: handlePotentialDateDoubleClick, // Use built-in date click handler
        eventClick: handlePotentialEventDoubleClick, // Use built-in event click handler

        // ── Drag-to-entity: register with DragTransfer when FC drag starts ──
        eventDragStart: function(info) {
            if (!window.DragTransfer) return;
            const ep = info.event.extendedProps || {};
            if (ep.source === 'cron') return; // cron events have no persistent file
            const entityPath = (ep.entity_rel_path !== undefined && ep.entity_rel_path !== null)
                ? ep.entity_rel_path : currentCalendarPath;
            window.DragTransfer.startCalendarWatch({
                type               : 'calendar',
                id                 : info.event.id,
                title              : info.event.title || '(untitled)',
                source_entity_path : entityPath,
                source_file        : null,
            });
        },

        // ── If dropped over the sidebar → let DragTransfer handle it ────────
        eventDragStop: function(info) {
            if (!window.DragTransfer) return;
            const handled = window.DragTransfer.checkCalendarDrop(info.jsEvent);
            if (handled) {
                // DragTransfer already moved the item; revert the FC placement
                info.revert();
            }
        },

        eventDrop: function(info) {
            // If a calendar-watch is still active when eventDrop fires it means
            // the drop landed on a valid calendar slot (not the sidebar).
            // Cancel the watch and proceed with the normal date-change save.
            if (window.DragTransfer) window.DragTransfer.stopCalendarWatch();
            handleEventDrop(info);
        },

        eventResize: handleEventResize,
        select: handleDateSelect, // Keep select for range selection if needed
        viewDidMount: handleViewChange, // Add handler for view changes
        datesSet: function(info) {
            // Re-inject the end-of-day 12am marker after every date navigation / view render
            setTimeout(injectEndOfDayLabel, 0);
        },
        eventContent: function(arg) { // Custom render function for event content
            // Show title and description if available
            let title = arg.event.title || '';
            let description = arg.event.extendedProps.description || '';
            let status = arg.event.extendedProps.status || '';

            let contentEl = document.createElement('div');
            contentEl.className = 'fc-event-content-wrapper';

            let html = `<div class="fc-event-title"><b>${title}</b></div>`;
            // Only show description inline for non-cron events
            // Cron events show title only in calendar; details visible on click
            let isCronEvent = arg.event.extendedProps.source === 'cron';
            if (description && !isCronEvent) {
                html += `<div class="fc-event-description">${description}</div>`;
            }

            // Add status indicator
            if (status === 'done') {
                html += `<div class="fc-event-status-indicator status-done">✔</div>`;
            } else if (status === 'cancelled') {
                html += `<div class="fc-event-status-indicator status-cancelled">✘</div>`;
            }

            contentEl.innerHTML = html;
            return { domNodes: [contentEl] };
        }
    });

    calendar.render();
}
function setupEventHandlers() {
    // Custom dblclick listener removed - using double-click detection on dateClick/eventClick

    // Close modal - find the cancel button specifically in the event modal
    const eventModalCancelBtn = eventModal.querySelector('.cancel-btn');
    const eventModalCloseBtn = eventModal.querySelector('.close-modal');

    if (eventModalCancelBtn) {
        eventModalCancelBtn.addEventListener('click', () => {
            // Immediately hide modal and set as closed for click handling
            eventModal.classList.remove('visible');
            eventModal.style.display = 'none'; // Force hide
            isEventModalOpen = false;

            // Reset double-click state
            lastClickTime = 0;
            lastClickTarget = null;
        });
    }

    if (eventModalCloseBtn) {
        eventModalCloseBtn.addEventListener('click', () => {
            // Immediately hide modal and set as closed for click handling
            eventModal.classList.remove('visible');
            eventModal.style.display = 'none'; // Force hide
            isEventModalOpen = false;

            // Reset double-click state
            lastClickTime = 0;
            lastClickTarget = null;
        });
    }

    // Submit event form
    eventForm.addEventListener('submit', handleEventSubmit);

    // Delete event
    deleteEventBtn.addEventListener('click', handleEventDelete);
}

function setupRefreshHandlers() {
    const refreshCalendarBtn = document.getElementById('refresh-calendar');
    if (refreshCalendarBtn) {
        refreshCalendarBtn.addEventListener('click', () => {
            console.log('Manual refresh calendar button clicked');
            reloadCalendarData(currentCalendarPath);
        });
    } else {
        console.warn('Refresh calendar button not found');
    }
}

// --- Double-Click Detection Handlers ---

// Called by FullCalendar's dateClick
function handlePotentialDateDoubleClick(info) {
    console.log('=== DATE CLICK START ===');
    console.log('Date click detected:', info.dateStr);
    console.log('Modal visible?', document.getElementById('event-modal').classList.contains('visible'));

    // Ignore clicks when modal is visible to prevent interference
    if (isEventModalOpen) {
        console.log('Modal is open (flag check), ignoring click');
        console.log('=== DATE CLICK END (IGNORED) ===');
        return;
    }

    const now = new Date().getTime();
    const targetDateStr = info.dateStr; // Use dateStr for consistent comparison

    console.log(`Click time: ${now}, lastClickTime: ${lastClickTime}, lastClickTarget: ${lastClickTarget}`);
    console.log(`Time diff: ${now - lastClickTime}, threshold: ${DOUBLE_CLICK_THRESHOLD}`);

    if (now - lastClickTime < DOUBLE_CLICK_THRESHOLD && lastClickTarget === targetDateStr) {
        // Double click detected
        console.log('Double click detected!');
        console.log('=== DATE CLICK END (DOUBLE_CLICK) ===');
        // Reset last click info IMMEDIATELY to prevent stuck state if modal fails
        lastClickTime = 0;
        lastClickTarget = null;

        try {
            handleNewEvent(info.date); // Pass the actual Date object to the handler
        } catch (e) {
            console.error("Error handling new event:", e);
        }
    } else {
        // Single click, record it
        console.log('Single click, recording');
        console.log('=== DATE CLICK END (SINGLE_CLICK) ===');
        lastClickTime = now;
        lastClickTarget = targetDateStr;
    }
}

// Called by FullCalendar's eventClick
function handlePotentialEventDoubleClick(info) {
    const now = new Date().getTime();
    const targetEventId = info.event.id;

    // Prevent default browser action (if the event is a link)
    info.jsEvent.preventDefault();

    // Subtle highlight: remove highlight from all events, add to clicked
    document.querySelectorAll('.fc-event-highlighted').forEach(el => {
        el.classList.remove('fc-event-highlighted');
    });
    info.el.classList.add('fc-event-highlighted');

    if (now - lastClickTime < DOUBLE_CLICK_THRESHOLD && lastClickTarget === targetEventId) {
        // Double click detected
        // Reset last click info IMMEDIATELY to prevent stuck state if modal fails
        lastClickTime = 0;
        lastClickTarget = null;
        
        try {
            handleEditEvent(info.event); // Pass the event object
        } catch (e) {
            console.error("Error handling edit event:", e);
        }
    } else {
        // Single click, record it
        lastClickTime = now;
        lastClickTarget = targetEventId;

        // Load into editor on single click as well
        const event = info.event;
        if (typeof loadCalendarEventForEditor === 'function') {
            const _ep1 = event.extendedProps || {};
            const eventDataForEditor = {
                id: event.id,
                title: event.title,
                start: event.startStr,
                end: event.endStr,
                allDay: event.allDay,
                backgroundColor: event.backgroundColor,
                borderColor: event.borderColor,
                extendedProps: JSON.parse(JSON.stringify(_ep1)),
                // Use per-event entity path stamped by the API; fall back to current view path
                entityPath: (_ep1.entity_rel_path !== undefined && _ep1.entity_rel_path !== null)
                    ? _ep1.entity_rel_path
                    : currentCalendarPath
            };
            loadCalendarEventForEditor(eventDataForEditor);
        }
    }
}


// --- Modal/Form Handlers (Now called by double-click detectors) ---


// Handle creating a new event (called on date double-click)
function handleNewEvent(date) { // Accepts a Date object
    console.log("Handling new event for date:", date);
    
    // Set default times (use the exact clicked time, do not round)
    const startDate = new Date(date);
    // If it was an all-day click (midnight), set to current hours/minutes or default to 9 AM
    if (startDate.getHours() === 0 && startDate.getMinutes() === 0 && startDate.getSeconds() === 0) {
        const now = new Date();
        // If clicked day is today, use current time
        if (startDate.toDateString() === now.toDateString()) {
            startDate.setHours(now.getHours());
            startDate.setMinutes(Math.ceil(now.getMinutes() / 15) * 15); // Round to nearest 15m
        } else {
            // Otherwise default to 9 AM
            startDate.setHours(9, 0, 0);
        }
    }
    
    const endDate = new Date(startDate);
    endDate.setHours(endDate.getHours() + 1);
    
    // Reset and populate form (wrap in try-catch inside to be safe)
    try {
        resetEventForm();
        eventStartInput.value = formatDateTimeForInput(startDate);
        eventEndInput.value = formatDateTimeForInput(endDate);
        
        // Show modal
        showEventModal('Create Event');
    } catch (e) {
        console.error("Error setting up event form:", e);
    }
}

// Handle editing an event (called on event double-click)
function handleEditEvent(event) { // Accepts a FullCalendar Event object
    // Populate event modal form with event data
    eventTitleInput.value = event.title;
    eventStartInput.value = formatDateTimeForInput(event.start);
    eventEndInput.value = formatDateTimeForInput(event.end || event.start);
    eventColorInput.value = event.backgroundColor || '#3788d8';
    eventDescriptionInput.value = event.extendedProps.description || '';
    eventIdInput.value = event.id;
    
    // Show event modal with delete button
    showEventModal('Edit Event');
    deleteEventBtn.style.display = 'block';

    // Also load this event into the main task editor
    if (typeof loadCalendarEventForEditor === 'function') {
        // Pass a copy of the event object to avoid direct modification issues
        // and include the currentCalendarPath for context.
        const eventDataForEditor = {
            id: event.id,
            title: event.title,
            start: event.startStr, // Use ISO string
            end: event.endStr,     // Use ISO string
            allDay: event.allDay,
            backgroundColor: event.backgroundColor,
            borderColor: event.borderColor,
            extendedProps: JSON.parse(JSON.stringify(event.extendedProps || {})), // Deep copy
            // Use per-event entity path stamped by the API; fall back to current view path
            entityPath: ((event.extendedProps || {}).entity_rel_path !== undefined && (event.extendedProps || {}).entity_rel_path !== null)
                ? event.extendedProps.entity_rel_path
                : currentCalendarPath
        };
        loadCalendarEventForEditor(eventDataForEditor);
    } else {
        console.warn("loadCalendarEventForEditor function not found in editor.js or not yet loaded.");
    }
}

// Date range was selected
function handleDateSelect(info) {
    // If it's a date click (not a range selection), just return
    if (!info.end || info.start.getTime() === info.end.getTime()) {
        return;
    }
    
    // Logic to open modal on date range selection removed.
    // Double-click is now the only way to open the create/edit modal.
    
    // Optionally, keep the selection visually highlighted until the user clicks elsewhere
    // or explicitly clear it:
    // calendar.unselect();
    
    // Clear selection
    calendar.unselect();
}

// Handle view change to update the active button indicator
function handleViewChange(info) {
    // Add depressed class to active button for visual indication
    document.querySelectorAll('.fc-button-active').forEach(button => {
        button.classList.add('fc-button-depressed');
    });
}

// Event was dragged to a new date/time
function handleEventDrop(info) {
    const event = info.event;
    // Use the event's own entity path (stamped by the API) so that events
    // belonging to a sub-entity are updated in the correct file even when
    // the root entity view is active.
    const ep = event.extendedProps || {};
    const eventEntityPath = (ep.entity_rel_path !== undefined && ep.entity_rel_path !== null)
        ? ep.entity_rel_path
        : currentCalendarPath;
    
    updateEvent({
        id: event.id,
        title: event.title,
        start: event.start.toISOString(),
        end: event.end ? event.end.toISOString() : null,
        backgroundColor: event.backgroundColor,
        // Preserve existing extendedProps, including status
        extendedProps: JSON.parse(JSON.stringify(ep))
    }, eventEntityPath).catch(() => {
        info.revert(); // Revert the drag if the update fails
    });
}

// Event duration was changed by resizing
function handleEventResize(info) {
    const event = info.event;
    // Use the event's own entity path so resizes also update the correct file
    const ep = event.extendedProps || {};
    const eventEntityPath = (ep.entity_rel_path !== undefined && ep.entity_rel_path !== null)
        ? ep.entity_rel_path
        : currentCalendarPath;
    
    updateEvent({
        id: event.id,
        title: event.title,
        start: event.start.toISOString(),
        end: event.end.toISOString(),
        backgroundColor: event.backgroundColor,
        // Preserve existing extendedProps, including status
        extendedProps: JSON.parse(JSON.stringify(ep))
    }, eventEntityPath).catch(() => {
        info.revert(); // Revert the resize if the update fails
    });
}

// Event form was submitted
async function handleEventSubmit(e) {
    e.preventDefault();
    
    // Collect form data
    const eventData = {
        title: eventTitleInput.value,
        start: new Date(eventStartInput.value).toISOString(),
        end: new Date(eventEndInput.value).toISOString(),
        backgroundColor: eventColorInput.value,
        borderColor: eventColorInput.value,
        // Ensure extendedProps is an object and include description and a default status
        extendedProps: {
            description: eventDescriptionInput.value,
            // If editing, preserve existing status, otherwise default.
            // The main editor will handle more complex status changes.
            status: eventIdInput.value && calendar.getEventById(eventIdInput.value)?.extendedProps?.status 
                    ? calendar.getEventById(eventIdInput.value).extendedProps.status 
                    : 'tentative' 
        }
    };
    
    try {
        if (eventIdInput.value) {
            // Update existing event
            eventData.id = eventIdInput.value;
            await updateEvent(eventData);
        } else {
            // Create new event
            eventData.id = generateEventId(eventData.start);
            await createEvent(eventData);
        }
        
        // Close modal and refresh calendar
        eventModal.classList.remove('visible');
        eventModal.style.display = 'none'; // Force hide
        // Reset modal flag and click state after successful submission
        isEventModalOpen = false;
        lastClickTime = 0;
        lastClickTarget = null;
        calendar.refetchEvents();
        
    } catch (error) {
        console.error('Error saving event:', error);
        alert('Failed to save event. Please try again.');
    }
}

// Delete event button was clicked
async function handleEventDelete() {
    const eventId = eventIdInput.value;
    
    if (!eventId) return;
    
    if (confirm('Are you sure you want to delete this event?')) {
        try {
            await deleteEvent(eventId);
            
            // Close modal and refresh calendar
            eventModal.classList.remove('visible');
            eventModal.style.display = 'none'; // Force hide
            isEventModalOpen = false;
            lastClickTime = 0;
            lastClickTarget = null;
            calendar.refetchEvents();
            
        } catch (error) {
            console.error('Error deleting event:', error);
            alert('Failed to delete event. Please try again.');
        }
    }
}

// API Functions
async function createEvent(eventData) {
    // Include the current path in the request
    const apiUrl = `/api/calendar?path=${encodeURIComponent(currentCalendarPath)}`;
    const response = await fetch(apiUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(eventData)
    });
    
    if (!response.ok) {
        throw new Error('Failed to create event');
    }
    
    return response.json();
}

async function updateEvent(eventData, entityPath) {
    // Include the event's own entity path in the request so sub-entity events
    // are saved to the correct file. Falls back to currentCalendarPath.
    const pathToUse = (entityPath !== undefined && entityPath !== null) ? entityPath : currentCalendarPath;
    const apiUrl = `/api/calendar?path=${encodeURIComponent(pathToUse)}`;
    const response = await fetch(apiUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(eventData)
    });
    
    if (!response.ok) {
        throw new Error('Failed to update event');
    }
    
    return response.json();
}

async function deleteEvent(eventId) {
    // Include the current path in the request
    const apiUrl = `/api/calendar/${eventId}?path=${encodeURIComponent(currentCalendarPath)}`;
    const response = await fetch(apiUrl, {
        method: 'DELETE'
    });
    
    if (!response.ok) {
        throw new Error('Failed to delete event');
    }
    
    return response.json();
}

// Helper Functions
function showEventModal(title) {
    // Check if modal element exists
    if (!eventModal) {
        console.error('ERROR: eventModal element not found!');
        return;
    }

    // Update modal title
    const modalTitle = eventModal.querySelector('h2');
    if (modalTitle) {
        modalTitle.textContent = title;
    }

    // Show/hide delete button
    deleteEventBtn.style.display = eventIdInput.value ? 'block' : 'none';

    // Set modal as open before showing it
    isEventModalOpen = true;

    // Show modal - use both class and direct style for reliability
    eventModal.classList.add('visible');
    eventModal.style.display = 'flex'; // Force display: flex for reliability
}

function resetEventForm() {
    eventForm.reset();
    eventIdInput.value = '';
    eventColorInput.value = '#3788d8';
}

// Function to reload calendar data for a specific path
function reloadCalendarData(entityPath = currentCalendarPath) {
    console.log(`Reloading calendar data for path: "${entityPath}"`);
    currentCalendarPath = entityPath || ''; // Update the stored path
    if (calendar) {
        calendar.refetchEvents(); // Refetch events using the updated path in the events function
        calendar.updateSize(); // Ensure calendar re-renders correctly if it was hidden
    }
}

// Highlight a calendar event by ID, scroll it into view, and load it into the editor
function highlightCalendarEventById(eventId) {
    if (!calendar || !eventId) return;
    // Remove existing highlights
    document.querySelectorAll('.fc-event-highlighted').forEach(el => {
        el.classList.remove('fc-event-highlighted');
    });

    const fcEvent = calendar.getEventById(eventId);
    if (!fcEvent) return;

    // Find and highlight the DOM element
    const eventEls = document.querySelectorAll('.fc-event');
    for (const el of eventEls) {
        const eventContent = el.querySelector('.fc-event-title');
        if (eventContent && eventContent.textContent.includes(fcEvent.title)) {
            el.classList.add('fc-event-highlighted');
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            break;
        }
    }

    // Also load the event into the Task Editor (just like a single click would)
    if (typeof loadCalendarEventForEditor === 'function') {
        const _ep = fcEvent.extendedProps || {};
        const eventDataForEditor = {
            id: fcEvent.id,
            title: fcEvent.title,
            start: fcEvent.start ? fcEvent.start.toISOString() : '',
            end: fcEvent.end ? fcEvent.end.toISOString() : '',
            allDay: fcEvent.allDay,
            backgroundColor: fcEvent.backgroundColor,
            borderColor: fcEvent.borderColor,
            extendedProps: JSON.parse(JSON.stringify(_ep)),
            entityPath: (_ep.entity_rel_path !== undefined && _ep.entity_rel_path !== null)
                ? _ep.entity_rel_path
                : currentCalendarPath
        };
        loadCalendarEventForEditor(eventDataForEditor);
    }
}
window.highlightCalendarEventById = highlightCalendarEventById;

function formatDateTimeForInput(date) {
    if (!date) return '';
    
    const d = new Date(date);
    
    // Format as YYYY-MM-DDTHH:MM (required format for datetime-local input)
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    const hours = String(d.getHours()).padStart(2, '0');
    const minutes = String(d.getMinutes()).padStart(2, '0');
    
    return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function generateEventId(startDate) {
    // Generate a timestamp-based ID with cal_ prefix and date/time info
    const now = new Date();
    const date = startDate ? new Date(startDate) : now;
    
    // Format: cal_YYYYMMDD_HHMMSS_timestamp
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const seconds = String(date.getSeconds()).padStart(2, '0');
    
    return `cal_${year}${month}${day}_${hours}${minutes}${seconds}_${Date.now()}`;
}

/**
 * Inject a "12am" end-of-day label row at the bottom of the timeGrid week/day views.
 * The row gets a double-top-border to mirror the midnight double-line at the top of the grid.
 * Called via datesSet so it re-runs on every navigation / view switch.
 */
function injectEndOfDayLabel() {
    if (!calendar) return;
    const view = calendar.view;
    if (!view || !view.type.startsWith('timeGrid')) return;

    // Remove any previously injected markers (avoid duplicates after navigation)
    calendarEl.querySelectorAll('.fc-end-of-day-marker').forEach(el => el.remove());

    // Find the slots table – we need its pixel height *before* appending the 12am row.
    const slotsTableBody = calendarEl.querySelector('.fc-timegrid-slots table tbody');
    if (!slotsTableBody) return;
    const slotsTable = slotsTableBody.closest('table');

    // Measure the current bottom of the last slot (= top of the future 12am row).
    const separatorTop = slotsTable ? slotsTable.offsetHeight : null;

    // 1. Add the "12am" label row to the narrow time-axis column (left side).
    const tr = document.createElement('tr');
    tr.className = 'fc-end-of-day-marker';
    tr.setAttribute('data-time', '24:00:00');
    const td = document.createElement('td');
    td.className = 'fc-timegrid-slot fc-timegrid-slot-label fc-scrollgrid-shrink';
    td.setAttribute('data-time', '24:00:00');
    td.innerHTML =
        '<div class="fc-timegrid-slot-label-frame fc-scrollgrid-shrink-frame">' +
        '<span class="fc-timegrid-slot-label-cushion fc-scrollgrid-shrink-cushion">12am</span>' +
        '</div>';
    tr.appendChild(td);
    slotsTableBody.appendChild(tr);

    // 2. Inject a full-width separator into .fc-timegrid-body (position:relative),
    //    pinned via the measured pixel offset so it spans both the time-axis and
    //    event-columns areas at exactly the 12am mark.
    if (separatorTop !== null) {
        const timegridBody = calendarEl.querySelector('.fc-timegrid-body');
        if (timegridBody) {
            const sep = document.createElement('div');
            sep.className = 'fc-end-of-day-marker fc-end-of-day-separator';
            sep.style.top = separatorTop + 'px';

            // Mirror the exact border color FullCalendar is using for its own grid lines
            // so the separator follows the active theme automatically.
            const sampleSlot = calendarEl.querySelector('.fc-timegrid-slot');
            if (sampleSlot) {
                const liveColor = getComputedStyle(sampleSlot).borderTopColor;
                // Only apply if it's a real visible colour (not transparent)
                if (liveColor && liveColor !== 'rgba(0, 0, 0, 0)' && liveColor !== 'transparent') {
                    sep.style.borderTopColor = liveColor;
                }
            }

            timegridBody.appendChild(sep);
        }
    }
}
