(() => {
    const form = document.getElementById('resetPasswordForm');
    const tokenField = document.getElementById('resetToken');
    const prompt = document.getElementById('resetLinkPrompt');
    if (!form || !tokenField || !prompt || tokenField.value) return;

    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const token = fragment.get('token');
    if (!token || token.length > 256) return;

    tokenField.value = token;
    form.hidden = false;
    prompt.hidden = true;
    window.history.replaceState(null, document.title, window.location.pathname);
})();
