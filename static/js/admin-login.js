(() => {
    const form = document.getElementById('adminLoginForm');
    const password = document.getElementById('adminPassword');
    const toggle = document.getElementById('togglePassword');
    const submit = document.getElementById('loginSubmit');
    const formError = document.getElementById('loginFormError');
    if (!form || !password || !toggle || !submit || !formError) return;

    toggle.addEventListener('click', () => {
        const reveal = password.type === 'password';
        password.type = reveal ? 'text' : 'password';
        toggle.textContent = reveal ? 'Hide' : 'Show';
        toggle.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
        toggle.setAttribute('aria-pressed', String(reveal));
        password.focus();
    });

    form.addEventListener('submit', event => {
        formError.hidden = true;
        formError.textContent = '';
        if (!form.checkValidity()) {
            event.preventDefault();
            form.reportValidity();
            return;
        }

        if (submit.disabled) {
            event.preventDefault();
            return;
        }
        submit.disabled = true;
        submit.classList.add('is-loading');
        submit.querySelector('.login-submit-label').textContent = 'Signing in...';
    });
})();
