/* AutoSub Dashboard - Interactive JavaScript Controller */

function showToast(message, type = 'success') {
    if (!message) return;
    
    let container = document.getElementById('toastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toastContainer';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    
    const toast = document.createElement('div');
    toast.className = 'toast';

    const icon = type === 'success' ? '✅' : '⚠️';

    const iconElement = document.createElement('div');
    iconElement.className = 'toast-icon';
    iconElement.textContent = icon;

    const textElement = document.createElement('div');
    textElement.className = 'toast-text';
    textElement.textContent = String(message);

    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'toast-close';
    closeButton.textContent = '×';
    closeButton.addEventListener('click', () => toast.remove());

    toast.append(iconElement, textElement, closeButton);
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.animation = 'toastFadeOut 0.4s forwards';
        setTimeout(() => toast.remove(), 400);
    }, 4500);
}

function setTagChecks(autoId, state) {
    const checkboxes = document.querySelectorAll(`input.tagcheck[data-auto="${autoId}"]`);
    checkboxes.forEach(cb => {
        cb.checked = state;
    });
    markFormDirty();
}

function filterTable(inputId, tableId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const filter = input.value.toLowerCase();
    const table = document.getElementById(tableId);
    if (!table) return;
    const trs = table.getElementsByTagName('tr');
    
    for (let i = 1; i < trs.length; i++) {
        const tr = trs[i];
        const text = tr.textContent || tr.innerText;
        if (text.toLowerCase().indexOf(filter) > -1) {
            tr.style.display = '';
        } else {
            tr.style.display = 'none';
        }
    }
}

function markFormDirty() {
    const statusText = document.getElementById('stickySaveStatus');
    const saveBtn = document.getElementById('stickySaveBtn');
    if (statusText) {
        statusText.textContent = '● Есть несохраненные изменения';
        statusText.style.color = '#f59e0b';
    }
    if (saveBtn) {
        saveBtn.style.boxShadow = '0 0 25px rgba(245, 158, 11, 0.5)';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Check for server flash message
    const msgElement = document.getElementById('serverFlashMessage');
    if (msgElement && msgElement.dataset.message) {
        showToast(msgElement.dataset.message, 'success');
    }
    
    // Listen for form changes
    const mainForm = document.getElementById('mainAdminForm');
    if (mainForm) {
        mainForm.addEventListener('change', markFormDirty);
        mainForm.addEventListener('input', markFormDirty);
    }
});
