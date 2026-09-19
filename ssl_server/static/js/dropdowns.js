/**
 * Dropdown toggle functionality for LOS Dashboard
 */

document.addEventListener('DOMContentLoaded', () => {
    // Dashboard dropdown toggle
    const dashboardDropdown = document.querySelector('.dashboard-dropdown');
    const dashboardDropdownBtn = document.querySelector('.dashboard-dropdown-btn');
    const dashboardDropdownContent = document.querySelector('.dashboard-dropdown-content');
    
    if (dashboardDropdownBtn && dashboardDropdownContent) {
        dashboardDropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            dashboardDropdownContent.classList.toggle('active');
        });
    }
    
    // Settings dropdown toggle
    const settingsDropdown = document.querySelector('.settings-dropdown');
    const settingsDropdownBtn = document.querySelector('.settings-dropdown-btn');
    const settingsDropdownContent = document.querySelector('.settings-dropdown-content');
    
    if (settingsDropdownBtn && settingsDropdownContent) {
        settingsDropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            settingsDropdownContent.classList.toggle('active');
        });
    }
    
    // User dropdown toggle
    const userDropdown = document.querySelector('.user-dropdown');
    const userDropdownBtn = document.querySelector('.user-dropdown-btn');
    const userDropdownContent = document.querySelector('.user-dropdown-content');
    
    if (userDropdownBtn && userDropdownContent) {
        userDropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            userDropdownContent.classList.toggle('active');
        });
    }
    
    // Close all dropdowns when clicking outside
    document.addEventListener('click', () => {
        if (dashboardDropdownContent) dashboardDropdownContent.classList.remove('active');
        if (settingsDropdownContent) settingsDropdownContent.classList.remove('active');
        if (userDropdownContent) userDropdownContent.classList.remove('active');
    });
});
