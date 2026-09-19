document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('agenda-sidebar');
    const toggleSidebarBtn = document.getElementById('toggle-agenda-sidebar-btn');
    const agendaTreeContainer = document.getElementById('agenda-tree');
    const addSubentityBtn = document.getElementById('add-subentity-btn');
    const subentityModal = document.getElementById('subentity-modal');
    const closeSubentityModalBtn = document.getElementById('close-subentity-modal');
    const cancelSubentityCreationBtn = document.getElementById('cancel-subentity-creation');
    const subentityForm = document.getElementById('subentity-form');
    const agendaSidebarTitle = document.getElementById('agenda-title');
    const mainViewTitle = document.getElementById('main-view-title');

    let currentAgendaPath = '';
    let currentUsername = '';
    let expandedDirectories = {};
    let currentEntityPath = '';

    // --- Sidebar Toggle ---
    toggleSidebarBtn.addEventListener('click', () => {
        sidebar.classList.toggle('hidden');
        document.body.classList.toggle('sidebar-visible');
        window.dispatchEvent(new Event('resize'));
    });

    // --- Modal Handling ---
    function openSubentityModal(parentPath) {
        currentAgendaPath = parentPath || '';
        document.getElementById('parent-entity-path').value = currentAgendaPath;
        fetchSkeletonData();
        subentityModal.classList.add('visible');
    }

    function closeSubentityModal() {
        subentityModal.classList.remove('visible');
        subentityForm.reset();
        document.getElementById('parent-entity-path').value = '';
        currentAgendaPath = '';
    }

    if (addSubentityBtn) {
        addSubentityBtn.addEventListener('click', () => {
            const parentPath = currentEntityPath || '';
            openSubentityModal(parentPath);
        });
    } else {
        console.error("Add Sub-entity button not found!");
    }

    closeSubentityModalBtn.addEventListener('click', closeSubentityModal);
    cancelSubentityCreationBtn.addEventListener('click', closeSubentityModal);
    subentityModal.addEventListener('click', (event) => {
        if (event.target === subentityModal) {
            closeSubentityModal();
        }
    });

    // --- API Calls ---
    async function fetchAgendaTree() {
        try {
            const response = await fetch('/api/agenda/tree');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const treeData = await response.json();
            if (treeData && treeData.length > 0) {
                 currentUsername = treeData[0].name;
                 if (mainViewTitle) {
                     mainViewTitle.textContent = `Agenda - /los/${currentUsername}`;
                 }
                 renderAgendaTree(treeData, agendaTreeContainer);
                 // Load files and directories for root
                 loadFilesAndDirectories('', agendaTreeContainer);
            } else {
                 agendaTreeContainer.innerHTML = '<p>No agenda structure found.</p>';
            }
        } catch (error) {
            console.error('Error fetching agenda tree:', error);
            agendaTreeContainer.innerHTML = '<p class="error-message">Failed to load agenda tree.</p>';
        }
    }

    async function fetchSkeletonData() {
        try {
            const response = await fetch('/api/agenda/skeleton');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const skeleton = await response.json();
            document.getElementById('subentity-identity').value = skeleton.identity || '';
            document.getElementById('subentity-goals').value = skeleton.goals || '';
            document.getElementById('subentity-omni').value = skeleton.omni || '';
        } catch (error) {
            console.error('Error fetching skeleton data:', error);
        }
    }

    async function createSubentity(formData) {
        try {
            const response = await fetch('/api/agenda/create', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(formData),
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
            }

            const result = await response.json();
            console.log('Sub-entity created:', result);
            closeSubentityModal();
            fetchAgendaTree();
            alert('Sub-entity created successfully!');

        } catch (error) {
            console.error('Error creating sub-entity:', error);
            alert(`Error creating sub-entity: ${error.message}`);
        }
    }

    async function loadFilesAndDirectories(entityPath, container) {
        try {
            const response = await fetch(`/api/agenda/files?path=${encodeURIComponent(entityPath)}`);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            renderFilesAndDirectories(data, container, entityPath);
        } catch (error) {
            console.error('Error fetching files and directories:', error);
        }
    }

    async function loadDirectoryContents(dirPath, container, parentLi) {
        try {
            const response = await fetch(`/api/agenda/directory/contents?path=${encodeURIComponent(dirPath)}`);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const contents = await response.json();
            renderDirectoryContents(contents, container, parentLi, dirPath);
        } catch (error) {
            console.error('Error fetching directory contents:', error);
        }
    }

    // --- Rendering ---
    function renderAgendaTree(nodes, container) {
        const existingUl = container.querySelector('ul.entity-tree');
        if (existingUl) {
            existingUl.remove();
        }

        const ul = document.createElement('ul');
        ul.classList.add('entity-tree');
        
        nodes.forEach(node => {
            const li = document.createElement('li');
            li.classList.add('entity-node');
            li.dataset.path = node.path;

            const nodeContentWrapper = document.createElement('div');
            nodeContentWrapper.classList.add('entity-node-content');

            const icon = document.createElement('i');
            icon.classList.add('fas');

            const nameSpan = document.createElement('span');
            nameSpan.textContent = node.name;

            nodeContentWrapper.appendChild(icon);
            nodeContentWrapper.appendChild(nameSpan);
            li.appendChild(nodeContentWrapper);

            // Check if node has children (sub-entities) to determine icon
            if (node.children && node.children.length > 0) {
                // fa-folder-plus signals the folder is expandable
                icon.classList.add('fa-folder-plus');
                icon.style.cursor = 'pointer';
                icon.title = 'Click to expand/collapse';

                const childrenUl = document.createElement('ul');
                childrenUl.style.display = 'none';
                renderAgendaTree(node.children, childrenUl);
                li.appendChild(childrenUl);

                // Clicking ONLY the folder icon toggles expand/collapse
                icon.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const isExpanded = childrenUl.style.display !== 'none';
                    childrenUl.style.display = isExpanded ? 'none' : 'block';
                    icon.classList.toggle('fa-folder-open', !isExpanded);
                    icon.classList.toggle('fa-folder-plus', isExpanded);
                });

                // Double-click on name → load this entity's agenda (also highlights it)
                nameSpan.addEventListener('dblclick', (e) => {
                    e.stopPropagation();
                    loadEntityAgenda(node);
                });

            } else {
                // Node has no children — plain folder icon, no expand/collapse
                icon.classList.add('fa-folder');

                // Double-click on name → load this entity's agenda (also highlights it)
                nameSpan.addEventListener('dblclick', (e) => {
                    e.stopPropagation();
                    loadEntityAgenda(node);
                });
            }

            ul.appendChild(li);
        });

        container.appendChild(ul);
    }

    function renderFilesAndDirectories(data, container, entityPath) {
        const existingSection = container.querySelector('.files-section');
        if (existingSection) {
            existingSection.remove();
        }

        const section = document.createElement('div');
        section.classList.add('files-section');
        section.dataset.entityPath = entityPath;

        const separator = document.createElement('hr');
        separator.classList.add('agenda-divider');
        section.appendChild(separator);

        if (data.files && data.files.length > 0) {
            const filesUl = document.createElement('ul');
            filesUl.classList.add('files-list');
            
            data.files.forEach(file => {
                const li = document.createElement('li');
                li.classList.add('file-item');
                li.dataset.filePath = file.path;

                const contentWrapper = document.createElement('div');
                contentWrapper.classList.add('content-item-wrapper');

                const icon = document.createElement('i');
                icon.classList.add('fas', 'fa-file');

                const nameSpan = document.createElement('span');
                nameSpan.textContent = file.name;

                contentWrapper.appendChild(icon);
                contentWrapper.appendChild(nameSpan);
                li.appendChild(contentWrapper);

                contentWrapper.addEventListener('click', () => {
                    viewFileInCanvas(file.path, file.name);
                });

                filesUl.appendChild(li);
            });

            section.appendChild(filesUl);
        }

        if (data.directories && data.directories.length > 0) {
            const dirsUl = document.createElement('ul');
            dirsUl.classList.add('directories-list');

            data.directories.forEach(dir => {
                const li = document.createElement('li');
                li.classList.add('directory-item');
                li.dataset.dirPath = dir.path;

                // Add CSS class only for data directories
                if (dir.name === 'data') {
                    li.classList.add('data-dir');
                }

                const contentWrapper = document.createElement('div');
                contentWrapper.classList.add('content-item-wrapper');

                const icon = document.createElement('i');
                icon.classList.add('fas', 'fa-folder');

                const nameSpan = document.createElement('span');
                nameSpan.textContent = dir.name;

                contentWrapper.appendChild(icon);
                contentWrapper.appendChild(nameSpan);
                li.appendChild(contentWrapper);

                const contentsUl = document.createElement('ul');
                contentsUl.classList.add('directory-contents');
                contentsUl.style.display = 'none';
                li.appendChild(contentsUl);

                contentWrapper.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const isExpanded = contentsUl.style.display !== 'none';
                    
                    if (isExpanded) {
                        contentsUl.style.display = 'none';
                        icon.classList.remove('fa-folder-open');
                        icon.classList.add('fa-folder');
                        li.classList.remove('expanded');
                    } else {
                        if (contentsUl.children.length === 0) {
                            loadDirectoryContents(dir.path, contentsUl, li);
                        }
                        contentsUl.style.display = 'block';
                        icon.classList.remove('fa-folder');
                        icon.classList.add('fa-folder-open');
                        li.classList.add('expanded');
                    }
                });

                dirsUl.appendChild(li);
            });

            section.appendChild(dirsUl);
        }

        container.appendChild(section);
    }

    function renderDirectoryContents(contents, container, parentLi, dirPath) {
        container.innerHTML = '';
        
        contents.forEach(item => {
            const li = document.createElement('li');
            li.classList.add('content-item');
            li.dataset.path = item.path;

            // Add CSS class only for data directories (keep others yellow)
            if (item.type === 'directory' && item.name === 'data') {
                li.classList.add('data-dir');
            }

            const contentWrapper = document.createElement('div');
            contentWrapper.classList.add('content-item-wrapper');

            const icon = document.createElement('i');
            if (item.type === 'file') {
                icon.classList.add('fas', 'fa-file');
            } else {
                icon.classList.add('fas', 'fa-folder');
            }

            const nameSpan = document.createElement('span');
            nameSpan.textContent = item.name;

            contentWrapper.appendChild(icon);
            contentWrapper.appendChild(nameSpan);
            li.appendChild(contentWrapper);

            if (item.type === 'file') {
                contentWrapper.addEventListener('click', (e) => {
                    e.stopPropagation();
                    viewFileInCanvas(item.path, item.name);
                });
            } else {
                const subContentsUl = document.createElement('ul');
                subContentsUl.classList.add('directory-contents');
                subContentsUl.style.display = 'none';
                li.appendChild(subContentsUl);

                contentWrapper.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const isExpanded = subContentsUl.style.display !== 'none';
                    
                    if (isExpanded) {
                        subContentsUl.style.display = 'none';
                        icon.classList.remove('fa-folder-open');
                        icon.classList.add('fa-folder');
                        li.classList.remove('expanded');
                    } else {
                        if (subContentsUl.children.length === 0) {
                            loadDirectoryContents(item.path, subContentsUl, li);
                        }
                        subContentsUl.style.display = 'block';
                        icon.classList.remove('fa-folder');
                        icon.classList.add('fa-folder-open');
                        li.classList.add('expanded');
                    }
                });
            }

            container.appendChild(li);
        });
    }

    // --- Canvas Functions ---
    async function viewFileInCanvas(filePath, fileName) {
        try {
            const response = await fetch(`/api/file/read?path=${encodeURIComponent(filePath)}`);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            updateCanvasView(filePath, fileName, data.content);
        } catch (error) {
            console.error('Error reading file:', error);
            alert('Failed to read file: ' + error.message);
        }
    }

    function getFileDescription(fileName) {
        const ext = fileName.split('.').pop().toLowerCase();
        const descriptions = {
            'txt': 'text file',
            'md': 'markdown file',
            'py': 'python file',
            'js': 'javascript file',
            'html': 'html file',
            'css': 'css file',
            'json': 'json file',
            'xml': 'xml file',
            'yaml': 'yaml file',
            'yml': 'yaml file',
            'sh': 'shell script',
            'bash': 'bash script',
            'sql': 'sql file',
            'log': 'log file'
        };
        return descriptions[ext] || 'file';
    }

    function updateCanvasView(filePath, fileName, content) {
        const canvasWindow = document.getElementById('additional-window');
        const canvasHeader = canvasWindow.querySelector('.window-header');
        const canvasContent = canvasWindow.querySelector('.window-content');

        // Update title
        const canvasTitle = canvasHeader.querySelector('#canvas-title');
        const fileDescription = getFileDescription(fileName);
        canvasTitle.innerHTML = `<i class="fas fa-file-alt"></i> ${fileName} (${fileDescription})`;

        // Show controls and reset to view mode
        const canvasHeaderControls = canvasHeader.querySelector('.canvas-header-controls');
        canvasHeaderControls.style.visibility = 'visible';
        const editBtn = document.getElementById('canvas-edit-btn');
        const saveBtn = document.getElementById('canvas-save-btn');
        const cancelBtn = document.getElementById('canvas-cancel-btn');
        if (editBtn) editBtn.style.display = 'inline-flex';
        if (saveBtn) saveBtn.style.display = 'none';
        if (cancelBtn) cancelBtn.style.display = 'none';

        // Set content
        canvasContent.innerHTML = `
            <textarea id="canvas-file-content" class="canvas-textarea view-mode" readonly>${escapeHtml(content)}</textarea>
        `;

        const originalContent = content;
        const textarea = document.getElementById('canvas-file-content');

        editBtn.addEventListener('click', () => {
            textarea.classList.remove('view-mode');
            textarea.classList.add('edit-mode');
            textarea.removeAttribute('readonly');
            editBtn.style.display = 'none';
            saveBtn.style.display = 'inline-flex';
            cancelBtn.style.display = 'inline-flex';
        });

        cancelBtn.addEventListener('click', () => {
            textarea.value = originalContent;
            textarea.classList.remove('edit-mode');
            textarea.classList.add('view-mode');
            textarea.setAttribute('readonly', 'true');
            editBtn.style.display = 'inline-flex';
            saveBtn.style.display = 'none';
            cancelBtn.style.display = 'none';
        });

        saveBtn.addEventListener('click', async () => {
            const newContent = textarea.value;
            try {
                const response = await fetch('/api/file/write', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        path: filePath,
                        content: newContent
                    }),
                });

                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }

                const result = await response.json();
                if (result.success) {
                    textarea.classList.remove('edit-mode');
                    textarea.classList.add('view-mode');
                    textarea.setAttribute('readonly', 'true');
                    editBtn.style.display = 'inline-flex';
                    saveBtn.style.display = 'none';
                    cancelBtn.style.display = 'none';
                    showToast('File saved successfully!');
                } else {
                    throw new Error(result.error || 'Failed to save file');
                }
            } catch (error) {
                console.error('Error saving file:', error);
                alert('Failed to save file: ' + error.message);
            }
        });
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function showToast(message) {
        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.textContent = message;
        document.body.appendChild(toast);
        toast.offsetHeight;
        toast.classList.add('show');
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => {
                toast.remove();
            }, 300);
        }, 3000);
    }

    // --- Selection and Agenda Loading ---
    let currentlySelectedNodeElement = null;
    
    function selectEntityNode(node) {
        const entityPath = node.path || '';
        if (currentlySelectedNodeElement) {
            currentlySelectedNodeElement.classList.remove('selected-entity');
        }
        const treeContainer = document.getElementById('agenda-tree');
        currentlySelectedNodeElement = treeContainer.querySelector(`li[data-path="${node.path}"] .entity-node-content`);
        if (currentlySelectedNodeElement) {
            currentlySelectedNodeElement.classList.add('selected-entity');
        }
    }

    function loadEntityAgenda(node) {
        currentEntityPath = node.path || '';
        const fullPath = `/los/${currentUsername}${currentEntityPath ? '/' + currentEntityPath : ''}`;
        if (mainViewTitle) {
            mainViewTitle.textContent = `Agenda - ${fullPath}`;
        }
        selectEntityNode(node);
        loadFilesAndDirectories(currentEntityPath, agendaTreeContainer);

        // Dispatch entity-selected event for AI window and other listeners
        document.dispatchEvent(new CustomEvent('entity-selected', {
            detail: { path: currentEntityPath, name: node.name }
        }));

        if (typeof window.reloadCalendarData === 'function') {
            window.reloadCalendarData(currentEntityPath);
        }
        if (typeof window.reloadTodoData === 'function') {
            window.reloadTodoData(currentEntityPath);
        }
        if (typeof window.reloadCrontabData === 'function') {
            window.reloadCrontabData(currentEntityPath);
        }
    }

    // Expose globally so notification clicks can switch entities
    window.loadEntityAgenda = loadEntityAgenda;
    window.getCurrentUsername = function() { return currentUsername; };
    window.getCurrentEntityPath = function() { return currentEntityPath; };

    // Expose fetchAgendaTree globally for drag-transfer.js
    window.fetchAgendaTree = fetchAgendaTree;

    /**
     * Highlight (select) a tree node by its entity path without re-fetching
     * or collapsing the tree.  Called by drag-transfer.js after a move.
     */
    window.selectAgendaEntityByPath = function(targetEntityPath) {
        const path  = targetEntityPath || '';
        const treeContainer = document.getElementById('agenda-tree');
        const li    = treeContainer.querySelector(`li[data-path="${path}"]`);
        if (!li) return; // node not currently rendered — nothing to do
        const node  = { path: path, name: path ? path.split('/').pop() : currentUsername };
        selectEntityNode(node);
    };

    /**
     * Refresh the agenda tree and then navigate to the given entity path.
     * (Kept for backward compatibility; not called by drag-transfer.js any more.)
     */
    window.fetchAgendaTreeAndSelect = function(targetEntityPath) {
        fetchAgendaTree().then(() => {
            const name = targetEntityPath
                ? targetEntityPath.split('/').pop()
                : currentUsername;
            loadEntityAgenda({ path: targetEntityPath || '', name: name });
        }).catch(err => {
            console.warn('fetchAgendaTreeAndSelect: tree refresh failed', err);
        });
    };

    subentityForm.addEventListener('submit', (event) => {
        event.preventDefault();
        const formData = {
            parent_path: document.getElementById('parent-entity-path').value,
            name: document.getElementById('subentity-name').value.trim(),
            identity: document.getElementById('subentity-identity').value.trim(),
            goals: document.getElementById('subentity-goals').value.trim(),
            omni: document.getElementById('subentity-omni').value.trim(),
            weight: parseInt(document.getElementById('subentity-weight').value, 10) || 100,
        };
        if (!formData.name || !/^[a-zA-Z0-9_-]+$/.test(formData.name)) {
             alert('Invalid Sub-entity name.');
             return;
        }
        createSubentity(formData);
    });

    fetchAgendaTree();
});
