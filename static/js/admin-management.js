(() => {
    const appMain = document.getElementById('appMain');
    const accountList = document.getElementById('adminAccountList');
    const auditList = document.getElementById('adminAuditLog');
    const notice = document.getElementById('adminAccountsNotice');
    const createForm = document.getElementById('createAdminForm');
    if (!appMain || !accountList || !auditList || !notice || !createForm) return;

    const csrfToken = appMain.dataset.csrfToken;
    const roles = [
        { value: 'admin', label: 'Administrator' },
        { value: 'admin_manager', label: 'Administrator manager' }
    ];

    function escapeHTML(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        })[character]);
    }

    function formatDate(value) {
        if (!value) return 'Never';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return 'Not recorded';
        return new Intl.DateTimeFormat(undefined, {
            dateStyle: 'medium',
            timeStyle: 'short'
        }).format(date);
    }

    function showNotice(message, isError = false) {
        notice.textContent = message;
        notice.classList.toggle('error', isError);
        notice.hidden = false;
    }

    async function sendRequest(url, method, payload) {
        const response = await fetch(url, {
            method,
            cache: 'no-store',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            body: payload === undefined ? undefined : JSON.stringify(payload)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            if (response.status === 401 || response.status === 403) {
                throw new Error(data.error || 'Your administrator session no longer has account-management access. Please sign in again.');
            }
            throw new Error(data.error || `Request failed (${response.status}).`);
        }
        return data;
    }

    function roleLabel(role) {
        return roles.find(item => item.value === role)?.label || 'Unknown role';
    }

    function renderAccounts(accounts) {
        if (!accounts.length) {
            accountList.innerHTML = '<tr><td colspan="6">No administrator accounts found.</td></tr>';
            return;
        }

        accountList.innerHTML = accounts.map(admin => `
            <tr data-admin-id="${Number(admin.id)}">
                <td>
                    <div class="admin-account-identity">
                        <strong>${escapeHTML(admin.full_name)}</strong>
                        <span>${escapeHTML(admin.email)}</span>
                    </div>
                    <form class="admin-account-edit" data-edit-account hidden>
                        <label>Full name
                            <input name="full_name" value="${escapeHTML(admin.full_name)}" maxlength="150" required>
                        </label>
                        <label>Email
                            <input name="email" type="email" value="${escapeHTML(admin.email)}" maxlength="254" required>
                        </label>
                        <button type="submit">Save name and email</button>
                    </form>
                </td>
                <td>
                    ${escapeHTML(roleLabel(admin.role))}
                    <select class="admin-role-select" aria-label="Role for ${escapeHTML(admin.full_name)}">
                        ${roles.map(role => `<option value="${role.value}"${admin.role === role.value ? ' selected' : ''}>${escapeHTML(role.label)}</option>`).join('')}
                    </select>
                    <button class="admin-secondary-button" type="button" data-account-action="role">Update role</button>
                </td>
                <td><span class="admin-account-status${admin.is_active ? '' : ' inactive'}">${admin.is_active ? 'Active' : 'Inactive'}</span></td>
                <td class="admin-account-date">${escapeHTML(formatDate(admin.created_at))}</td>
                <td class="admin-account-date">${escapeHTML(formatDate(admin.last_login))}</td>
                <td>
                    <div class="admin-account-actions">
                        <button type="button" data-account-action="edit">Edit</button>
                        <button type="button" data-account-action="toggle">${admin.is_active ? 'Deactivate' : 'Activate'}</button>
                        <button type="button" data-account-action="password">Reset password</button>
                    </div>
                    <form class="admin-password-reset" data-reset-password hidden>
                        <label>New password
                            <input name="password" type="password" autocomplete="new-password" minlength="12" required>
                        </label>
                        <label>Confirm password
                            <input name="confirm_password" type="password" autocomplete="new-password" minlength="12" required>
                        </label>
                        <button type="submit">Set new password</button>
                    </form>
                </td>
            </tr>`).join('');
    }

    function renderAuditLogs(entries) {
        if (!entries.length) {
            auditList.innerHTML = '<li>No administrator activity recorded yet.</li>';
            return;
        }
        auditList.innerHTML = entries.map(entry => {
            const details = Object.entries(entry.details || {})
                .map(([key, value]) => `${key.replaceAll('_', ' ')}: ${Array.isArray(value) ? value.join(', ') : value}`)
                .join(' · ');
            return `<li><strong>${escapeHTML(entry.actor)}</strong> ${escapeHTML(entry.action.replaceAll('_', ' '))}${entry.target ? ` for <strong>${escapeHTML(entry.target)}</strong>` : ''}${details ? ` (${escapeHTML(details)})` : ''}<br><span class="admin-account-date">${escapeHTML(formatDate(entry.created_at))}</span></li>`;
        }).join('');
    }

    async function loadAccounts() {
        try {
            const data = await sendRequest('/api/admin/accounts', 'GET');
            renderAccounts(data.admins);
            renderAuditLogs(data.audit_logs);
        } catch (error) {
            accountList.innerHTML = `<tr><td colspan="6">${escapeHTML(error.message)}</td></tr>`;
            auditList.innerHTML = '';
            showNotice(error.message, true);
        }
    }

    createForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (!createForm.reportValidity()) return;
        const formData = new FormData(createForm);
        const submit = createForm.querySelector('button[type="submit"]');
        submit.disabled = true;
        try {
            await sendRequest('/api/admin/accounts', 'POST', {
                full_name: formData.get('full_name'),
                email: formData.get('email'),
                password: formData.get('password'),
                role: formData.get('role')
            });
            createForm.reset();
            showNotice('Administrator account created.');
            await loadAccounts();
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            submit.disabled = false;
        }
    });

    accountList.addEventListener('click', async event => {
        const button = event.target.closest('[data-account-action]');
        if (!button) return;
        const row = button.closest('[data-admin-id]');
        const adminId = row.dataset.adminId;
        const action = button.dataset.accountAction;
        if (action === 'edit') {
            row.querySelector('[data-edit-account]').hidden = false;
            return;
        }
        if (action === 'password') {
            row.querySelector('[data-reset-password]').hidden = false;
            return;
        }
        button.disabled = true;
        try {
            if (action === 'role') {
                const role = row.querySelector('.admin-role-select').value;
                await sendRequest(`/api/admin/accounts/${adminId}/role`, 'PATCH', { role });
                showNotice('Administrator role updated.');
            } else if (action === 'toggle') {
                const isActive = row.querySelector('.admin-account-status').textContent.trim() !== 'Active';
                await sendRequest(`/api/admin/accounts/${adminId}/status`, 'PATCH', { is_active: isActive });
                showNotice(`Administrator ${isActive ? 'activated' : 'deactivated'}.`);
            }
            await loadAccounts();
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            button.disabled = false;
        }
    });

    accountList.addEventListener('submit', async event => {
        const form = event.target;
        const row = form.closest('[data-admin-id]');
        if (!row) return;
        event.preventDefault();
        if (!form.reportValidity()) return;
        const adminId = row.dataset.adminId;
        const submit = form.querySelector('button[type="submit"]');
        submit.disabled = true;
        try {
            if (form.matches('[data-edit-account]')) {
                const formData = new FormData(form);
                await sendRequest(`/api/admin/accounts/${adminId}`, 'PUT', {
                    full_name: formData.get('full_name'),
                    email: formData.get('email')
                });
                showNotice('Administrator details updated.');
            } else if (form.matches('[data-reset-password]')) {
                const formData = new FormData(form);
                const password = formData.get('password');
                if (password !== formData.get('confirm_password')) {
                    throw new Error('The passwords do not match.');
                }
                await sendRequest(`/api/admin/accounts/${adminId}/password`, 'POST', { password });
                showNotice('Administrator password reset successfully.');
            }
            await loadAccounts();
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            submit.disabled = false;
        }
    });

    document.getElementById('refreshAdminAccounts')
        .addEventListener('click', loadAccounts);
    loadAccounts();
})();
