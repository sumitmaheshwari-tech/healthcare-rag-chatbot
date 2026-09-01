/* ═══════════════════════════════════════════════════════════
   MedCare Assistant — Chat Client & Secure Authentication Logic
   ═══════════════════════════════════════════════════════════ */

// ── Configuration ────────────────────────────────────────────
const API_BASE = window.location.protocol === 'file:' ? 'http://localhost:8000' : window.location.origin;
let sessionId    = '';
let currentPatientId = '';      // Starts as empty/unauthenticated
let currentPatientName = '';
let isProcessing = false;

// ── DOM Elements ──────────────────────────────────────────────
let chatMessages, messageInput, sendBtn, quickActions, statusBadge;
let authLoggedOutContainer, authLoggedInContainer, authVerifiedText;
let logoutBtn;

// Auth Overlay Elements
let authOverlay, loginCard, registerCard;
let loginForm, registerForm;
let loginUidInput, loginDobInput, loginPhoneInput, loginErrorBox, loginSubmitBtn;
let loginCredentialsGroup, loginOtpGroup, loginOtpInput, loginTelegramLink, loginBackBtn;

let registerNameInput, registerDobInput, registerPhoneInput, registerErrorBox, registerSubmitBtn;
let registerCredentialsGroup, registerOtpGroup, registerOtpInput, registerTelegramLink, registerBackBtn;

let switchToRegisterLink, switchToLoginLink;
let profileSelectCard, profileListContainer, addFamilyMemberBtn, switchBackToLogin;
let lastVerifiedPhone = '';
let chatInputContainer;

let currentLoginSessionId = '';
let currentRegisterSessionId = '';

// ── Initialisation ───────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // 1. Bind DOM Elements safely
    chatMessages        = document.getElementById('chat-messages');
    messageInput        = document.getElementById('message-input');
    sendBtn             = document.getElementById('send-btn');
    quickActions        = document.getElementById('quick-actions');
    statusBadge         = document.getElementById('status-badge');

    authLoggedOutContainer = document.getElementById('auth-logged-out');
    authLoggedInContainer  = document.getElementById('auth-logged-in');
    authVerifiedText       = document.getElementById('auth-verified-text');
    logoutBtn              = document.getElementById('logout-btn');

    // Auth Gate Elements
    authOverlay         = document.getElementById('auth-overlay');
    loginCard           = document.getElementById('login-card');
    registerCard        = document.getElementById('register-card');
    loginForm           = document.getElementById('login-form');
    registerForm        = document.getElementById('register-form');
    
    loginUidInput       = document.getElementById('login-uid');
    loginDobInput       = document.getElementById('login-dob');
    loginPhoneInput     = document.getElementById('login-phone');
    loginErrorBox       = document.getElementById('login-error');
    loginSubmitBtn      = document.getElementById('login-submit-btn');
    loginCredentialsGroup = document.getElementById('login-credentials-group');
    loginOtpGroup       = document.getElementById('login-otp-group');
    loginOtpInput       = document.getElementById('login-otp');
    loginTelegramLink   = document.getElementById('login-telegram-link');
    loginBackBtn        = document.getElementById('login-back-btn');
    
    registerNameInput   = document.getElementById('register-name');
    registerDobInput    = document.getElementById('register-dob');
    registerPhoneInput  = document.getElementById('register-phone');
    registerErrorBox    = document.getElementById('register-error');
    registerSubmitBtn   = document.getElementById('register-submit-btn');
    registerCredentialsGroup = document.getElementById('register-credentials-group');
    registerOtpGroup    = document.getElementById('register-otp-group');
    registerOtpInput    = document.getElementById('register-otp');
    registerTelegramLink = document.getElementById('register-telegram-link');
        registerBackBtn     = document.getElementById('register-back-btn');
    profileSelectCard   = document.getElementById('profile-select-card');
    profileListContainer = document.getElementById('profile-list-container');
    addFamilyMemberBtn  = document.getElementById('add-family-member-btn');
    switchBackToLogin   = document.getElementById('switch-back-to-login');
    
    switchToRegisterLink = document.getElementById('switch-to-register');
    switchToLoginLink    = document.getElementById('switch-to-login');
    chatInputContainer   = document.getElementById('chat-input-container');

    // 2. Setup Event Handlers
    setupEventListeners();
    setupAuthFormListeners();

    // 3. Initialize session and check active login context
    sessionId = generateSessionId();
    checkAuthSession();
});

// ── Session & Auth State Checks ────────────────────────────────
function generateSessionId() {
    return 'sess-' + Date.now() + '-' + Math.random().toString(36).substring(2, 11);
}

async function checkAuthSession() {
    try {
        const res = await fetch(`${API_BASE}/api/patients/me`);
        if (res.ok) {
            const data = await res.json();
            if (data.success && data.patient) {
                loginPatient(data.patient);
                return;
            }
        }
    } catch (err) {
        console.error("Session check error:", err);
    }
    // If not authenticated, force the secure login gate
    logoutPatientUI();
}

// ── Send Message ─────────────────────────────────────────────
async function sendMessage(text) {
    if (isProcessing || !text.trim()) return;
    isProcessing = true;
    updateSendButton();

    // Show user message
    addMessage(text, 'user');
    messageInput.value = '';
    autoResize();

    // Show typing
    showTypingIndicator();

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 300000); // 5 min timeout

    try {
        const res = await fetch(`${API_BASE}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: text,
                session_id: sessionId
            }),
            signal: controller.signal,
        });

        if (!res.ok) {
            if (res.status === 401) {
                // Access expired or revoked, force re-login
                alert("Session expired. Please sign in again.");
                logoutPatientUI();
                isProcessing = false;
                updateSendButton();
                return;
            }
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Server error');
        }

        // Setup streaming reader
        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let botBubble = null;
        let botText = "";

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop(); // Keep last incomplete line in buffer

            for (const line of lines) {
                const cleanLine = line.trim();
                if (!cleanLine.startsWith("data:")) continue;

                let event;
                try {
                    const dataStr = cleanLine.substring(5).trim();
                    if (!dataStr) continue;

                    event = JSON.parse(dataStr);
                } catch (e) {
                    console.error("Failed to parse stream chunk:", e, cleanLine);
                    continue;
                }

                if (event.type === 'session') {
                    sessionId = event.session_id || sessionId;
                } else if (event.type === 'content') {
                    if (!botBubble) {
                        hideTypingIndicator();
                        botBubble = createEmptyBotMessage();
                    }
                    botText += event.text;
                    updateBotMessageContent(botBubble, botText);
                } else if (event.type === 'auth') {
                    // Succeeded login/registration during chat - call helper to set HttpOnly cookies
                    await fetch(`${API_BASE}/api/patients/set-session`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ patient_id: event.patient_id }),
                    });
                    // Set credentials in UI
                    currentPatientId = event.patient_id;
                    currentPatientName = event.patient_name;
                    authLoggedOutContainer.classList.add('hidden');
                    authLoggedInContainer.classList.remove('hidden');
                    authVerifiedText.innerHTML = `<i class="fas fa-user-check" style="color: #2f855a;"></i> Verified: <strong>${currentPatientName}</strong>`;
                } else if (event.type === 'error') {
                    hideTypingIndicator();
                    addMessage(event.message || 'An unknown error occurred.', 'bot', true);
                    break;
                }
            }
        }
        clearTimeout(timeoutId);
        hideTypingIndicator();

        // Empty-response detection
        if (!botBubble || !botText.trim()) {
            addMessage('I apologize, I was unable to generate a response. Please try again.', 'bot', true);
        }
    } catch (err) {
        clearTimeout(timeoutId);
        hideTypingIndicator();
        if (err.name === 'AbortError') {
            addMessage('The request timed out. Please try again.', 'bot', true);
        } else {
            addMessage(`Sorry, something went wrong: ${err.message}. Please try again.`, 'bot', true);
        }
    }

    isProcessing = false;
    updateSendButton();
}

function createEmptyBotMessage() {
    const row = document.createElement('div');
    row.className = 'message-row bot';

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerHTML = '<i class="fas fa-robot"></i>';

    const content = document.createElement('div');
    content.className = 'message-content';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.innerHTML = '<p class="typing-placeholder">...</p>';

    const time = document.createElement('span');
    time.className = 'message-time';
    time.textContent = formatTimestamp();

    content.appendChild(bubble);
    content.appendChild(time);
    row.appendChild(avatar);
    row.appendChild(content);
    chatMessages.appendChild(row);
    scrollToBottom();

    return bubble;
}

function updateBotMessageContent(bubble, text) {
    bubble.innerHTML = renderMarkdown(text);
    scrollToBottom();
}

// ── Message Rendering ────────────────────────────────────────
function addMessage(text, sender, isError = false) {
    const row = document.createElement('div');
    row.className = `message-row ${sender}${isError ? ' error' : ''}`;

    // Avatar
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerHTML = sender === 'bot'
        ? '<i class="fas fa-robot"></i>'
        : '<i class="fas fa-user"></i>';

    // Content wrapper
    const content = document.createElement('div');
    content.className = 'message-content';

    // Bubble
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.innerHTML = sender === 'bot' ? renderMarkdown(text) : escapeHtml(text);

    // Timestamp
    const time = document.createElement('span');
    time.className = 'message-time';
    time.textContent = formatTimestamp();

    content.appendChild(bubble);
    content.appendChild(time);
    row.appendChild(avatar);
    row.appendChild(content);

    chatMessages.appendChild(row);
    scrollToBottom();
}

function addWelcomeMessage() {
    chatMessages.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'message-row bot';
    row.innerHTML = `
        <div class="avatar"><i class="fas fa-robot"></i></div>
        <div class="message-content">
            <div class="message-bubble">
                <p>Hello! 👋 I'm <strong>MedCare Assistant</strong>, your secure AI healthcare companion.</p>
                <p>Since your profile is verified, I can assist you with booking appointments, checking your bills, viewing clinical visits, or finding general hospital guides.</p>
                <div class="welcome-features">
                    <span class="welcome-feature"><i class="fas fa-calendar-plus"></i> Book Appointments</span>
                    <span class="welcome-feature"><i class="fas fa-file-invoice-dollar"></i> Check Bills</span>
                    <span class="welcome-feature"><i class="fas fa-notes-medical"></i> Medical Records</span>
                    <span class="welcome-feature"><i class="fas fa-user-doctor"></i> Doctor Schedules</span>
                </div>
            </div>
            <span class="message-time">${formatTimestamp()}</span>
        </div>
    `;
    chatMessages.appendChild(row);
    scrollToBottom();
}

// ── Typing Indicator ─────────────────────────────────────────
function showTypingIndicator() {
    if (document.getElementById('typing-row')) return;

    const row = document.createElement('div');
    row.className = 'message-row bot';
    row.id = 'typing-row';
    row.innerHTML = `
        <div class="avatar"><i class="fas fa-robot"></i></div>
        <div class="message-content">
            <div class="typing-indicator">
                <div class="typing-dot"></div>
                <div class="typing-dot"></div>
                <div class="typing-dot"></div>
            </div>
        </div>
    `;
    chatMessages.appendChild(row);
    scrollToBottom();
}

// ── Lightweight Markdown Renderer ──────────────────────────
function renderMarkdown(text) {
    if (!text) return '';

    let html = escapeHtml(text);

    // Headings
    html = html.replace(/^####\s+(.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');

    // Horizontal rule
    html = html.replace(/^---$/gm, '<hr>');

    // Bold
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Italic
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Unordered lists
    html = html.replace(/^[\s]*[•\-\*]\s+(.+)$/gm, '<li>$1</li>');

    // Ordered lists
    html = html.replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>');

    // Wrap consecutive list items
    html = html.replace(/((?:<li>.*?<\/li>\n?)+)/g, '<ul>$1</ul>');

    // Links
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    // Paragraph breaks
    html = html.replace(/\n\n/g, '</p><p>');

    // Single line break
    html = html.replace(/\n/g, '<br>');

    html = '<p>' + html + '</p>';
    html = html.replace(/<p>\s*<\/p>/g, '');

    // Cleanup nested tags
    html = html.replace(/<p>(<ul>)/g, '$1');
    html = html.replace(/(<\/ul>)<\/p>/g, '$1');
    html = html.replace(/<p>(<h3|<h4)/g, '$1');
    html = html.replace(/(<\/h3>|<\/h4>)<\/p>/g, '$1');
    html = html.replace(/<p>(<hr>)<\/p>/g, '$1');

    return html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── Utilities ────────────────────────────────────────────────
def_none = () => {};

function hideTypingIndicator() {
    const el = document.getElementById('typing-row');
    if (el) el.remove();
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        chatMessages.scrollTo({ top: chatMessages.scrollHeight, behavior: 'smooth' });
    });
}

function formatTimestamp() {
    return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function autoResize() {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
}

function updateSendButton() {
    sendBtn.disabled = isProcessing || !messageInput.value.trim();
}

// ── Event Listeners ──────────────────────────────────────────
function setupEventListeners() {
    sendBtn.addEventListener('click', () => {
        sendMessage(messageInput.value);
    });

    messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage(messageInput.value);
        }
    });

    let prewarmTimer = null;
    messageInput.addEventListener('input', () => {
        autoResize();
        updateSendButton();

        // Proactive typing pre-warm heuristic (<300ms debounce)
        clearTimeout(prewarmTimer);
        const text = messageInput.value.trim();
        if (text.length >= 5) {
            prewarmTimer = setTimeout(() => {
                fetch(`${API_BASE}/api/chat/prewarm`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: text })
                }).catch(() => {});
            }, 300);
        }
    });

    quickActions.addEventListener('click', (e) => {
        const chip = e.target.closest('.action-chip');
        if (chip && !isProcessing) {
            const msg = chip.dataset.message;
            if (msg) sendMessage(msg);
        }
    });

    logoutBtn.addEventListener('click', () => {
        logoutPatientSession();
    });
}

// ── Phone & Recaptcha Helpers ─────────────────────────────────
// ── Gated Authentication Form Listeners ───────────────────────

// ── Family Profile Selector Helpers ───────────────────────────
function showFamilyProfileSelector(profiles, phone) {
    lastVerifiedPhone = phone || '';
    loginCard.classList.add('hidden');
    registerCard.classList.add('hidden');
    profileSelectCard.classList.remove('hidden');

    if (!profiles || profiles.length === 0) {
        profileListContainer.innerHTML = '<p style="color:#718096;text-align:center;">No profiles found.</p>';
        return;
    }

    profileListContainer.innerHTML = profiles.map(p => `
        <div class="profile-card-item" onclick="chooseProfile('${p.patient_uid}', '${escapeHtml(p.name)}')">
            <div class="profile-info">
                <div class="profile-name"><i class="fas fa-user-circle" style="color:#2b6cb0;margin-right:6px;"></i> ${escapeHtml(p.name)}</div>
                <div class="profile-sub">Patient ID: <code>${p.patient_uid}</code> ${p.dob ? ' • DOB: ' + p.dob : ''}</div>
            </div>
            <i class="fas fa-chevron-right profile-select-arrow"></i>
        </div>
    `).join('');
}

async function chooseProfile(patientUid, patientName) {
    try {
        const res = await fetch(`${API_BASE}/api/patients/select-profile`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ patient_uid: patientUid })
        });
        if (!res.ok) throw new Error('Failed to select profile.');
        const data = await res.json();
        profileSelectCard.classList.add('hidden');
        loginPatient({
            patient_uid: data.patient_uid,
            name: data.name
        });
    } catch(err) {
        alert(err.message || 'Error selecting profile.');
    }
}
window.chooseProfile = chooseProfile;

function escapeHtml(str) {
    return (str || '').replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[m]);
}

function setupAuthFormListeners() {
    // 1. Toggle switch between cards
    switchToRegisterLink.addEventListener('click', (e) => {
        e.preventDefault();
        loginCard.classList.add('hidden');
        profileSelectCard.classList.add('hidden');
        registerCard.classList.remove('hidden');
        loginErrorBox.classList.add('hidden');
        registerErrorBox.classList.add('hidden');
    });

    switchToLoginLink.addEventListener('click', (e) => {
        e.preventDefault();
        registerCard.classList.add('hidden');
        profileSelectCard.classList.add('hidden');
        loginCard.classList.remove('hidden');
        registerErrorBox.classList.add('hidden');
        loginErrorBox.classList.add('hidden');
    });

    if (switchBackToLogin) {
        switchBackToLogin.addEventListener('click', (e) => {
            e.preventDefault();
            profileSelectCard.classList.add('hidden');
            loginCard.classList.remove('hidden');
            loginCredentialsGroup.classList.remove('hidden');
            loginOtpGroup.classList.add('hidden');
            loginSubmitBtn.textContent = '📲 Get OTP on Telegram';
        });
    }

    if (addFamilyMemberBtn) {
        addFamilyMemberBtn.addEventListener('click', (e) => {
            e.preventDefault();
            profileSelectCard.classList.add('hidden');
            registerCard.classList.remove('hidden');
            registerCredentialsGroup.classList.remove('hidden');
            registerOtpGroup.classList.add('hidden');
            registerSubmitBtn.textContent = '📲 Get OTP on Telegram';
            if (lastVerifiedPhone) {
                registerPhoneInput.value = lastVerifiedPhone;
            }
            registerNameInput.value = '';
            registerNameInput.focus();
        });
    }

    // 2. Telegram Sign In Form (Mobile Phone -> Telegram OTP -> Family Selector)
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        loginErrorBox.classList.add('hidden');

        const uid = loginUidInput.value.trim();
        const dob = loginDobInput.value.trim();
        const phone = loginPhoneInput.value.trim();

        // Step 1: Initiate Telegram OTP Session
        if (loginOtpGroup.classList.contains('hidden')) {
            loginSubmitBtn.disabled = true;
            loginSubmitBtn.textContent = 'Opening Telegram Gateway...';

            try {
                const res = await fetch(`${API_BASE}/api/auth/telegram/initiate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        flow: 'login',
                        patient_uid: uid,
                        dob: dob,
                        phone: phone
                    })
                });

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'No account found with this phone number. Please click "Register profile here".');
                }

                const data = await res.json();
                currentLoginSessionId = data.auth_session_id;

                if (loginTelegramLink && data.telegram_url) {
                    loginTelegramLink.href = data.telegram_url;
                    window.open(data.telegram_url, '_blank');
                }

                loginCredentialsGroup.classList.add('hidden');
                loginOtpGroup.classList.remove('hidden');
                loginOtpInput.required = true;
                loginOtpInput.focus();
                loginSubmitBtn.disabled = false;
                loginSubmitBtn.textContent = 'Verify Code & Sign In';
            } catch (err) {
                loginErrorBox.textContent = err.message;
                loginErrorBox.classList.remove('hidden');
                loginSubmitBtn.disabled = false;
                loginSubmitBtn.textContent = '📲 Get OTP on Telegram';
            }
        }
        // Step 2: Verify Telegram OTP
        else {
            const otp = loginOtpInput.value.trim();
            loginSubmitBtn.disabled = true;
            loginSubmitBtn.textContent = 'Verifying Code...';

            try {
                const res = await fetch(`${API_BASE}/api/auth/telegram/verify`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        auth_session_id: currentLoginSessionId,
                        otp: otp
                    })
                });

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Invalid or expired OTP code. Please tap START in @MedCare_Verify_Auth_bot.');
                }

                const data = await res.json();
                if (!data.multiple_profiles) {
                    loginPatient({
                        patient_uid: data.patient_uid,
                        name: data.name
                    });
                } else {
                    showFamilyProfileSelector(data.profiles, data.phone || phone);
                }
            } catch (err) {
                loginErrorBox.textContent = err.message;
                loginErrorBox.classList.remove('hidden');
                loginSubmitBtn.disabled = false;
                loginSubmitBtn.textContent = 'Verify Code & Sign In';
            }
        }
    });

    // 3. Telegram Register Form (Name + DOB + Mobile Phone -> Telegram OTP)
    registerForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        registerErrorBox.classList.add('hidden');

        const name = registerNameInput.value.trim();
        const dob = registerDobInput.value.trim();
        const phone = registerPhoneInput.value.trim();

        if (registerOtpGroup.classList.contains('hidden')) {
            registerSubmitBtn.disabled = true;
            registerSubmitBtn.textContent = 'Opening Telegram Gateway...';

            try {
                const res = await fetch(`${API_BASE}/api/auth/telegram/initiate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        flow: 'register',
                        name: name,
                        dob: dob,
                        phone: phone
                    })
                });

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Registration failed. Please check your details.');
                }

                const data = await res.json();
                currentRegisterSessionId = data.auth_session_id;

                if (registerTelegramLink && data.telegram_url) {
                    registerTelegramLink.href = data.telegram_url;
                    window.open(data.telegram_url, '_blank');
                }

                registerCredentialsGroup.classList.add('hidden');
                registerOtpGroup.classList.remove('hidden');
                registerOtpInput.required = true;
                registerOtpInput.focus();
                registerSubmitBtn.disabled = false;
                registerSubmitBtn.textContent = 'Verify Code & Complete Registration';
            } catch (err) {
                registerErrorBox.textContent = err.message;
                registerErrorBox.classList.remove('hidden');
                registerSubmitBtn.disabled = false;
                registerSubmitBtn.textContent = '📲 Get OTP on Telegram';
            }
        }
        else {
            const otp = registerOtpInput.value.trim();
            registerSubmitBtn.disabled = true;
            registerSubmitBtn.textContent = 'Creating Profile...';

            try {
                const res = await fetch(`${API_BASE}/api/auth/telegram/verify`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        auth_session_id: currentRegisterSessionId,
                        otp: otp
                    })
                });

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Invalid or expired OTP code. Please tap START in @MedCare_Verify_Auth_bot.');
                }

                const regData = await res.json();

                loginPatient({
                    patient_uid: regData.patient_uid,
                    name: regData.name,
                    dob: regData.dob
                });

                addMessage(
                    `🎉 **Welcome to MedCare Hospital, ${regData.name}!**

` +
                    `Your identity has been verified via Telegram and your profile is active.

` +
                    `🔑 **Your Official Patient ID is:** \`${regData.patient_uid}\`

` +
                    `*(You can now book appointments, view doctor availability, and check medical records anytime!)*`,
                    'bot'
                );
            } catch (err) {
                registerErrorBox.textContent = err.message;
                registerErrorBox.classList.remove('hidden');
                registerSubmitBtn.disabled = false;
                registerSubmitBtn.textContent = 'Verify Code & Complete Registration';
            }
        }
    });

    // 4. Back Button Handlers
    if (loginBackBtn) {
        loginBackBtn.addEventListener('click', (e) => {
            e.preventDefault();
            loginOtpGroup.classList.add('hidden');
            loginCredentialsGroup.classList.remove('hidden');
            loginOtpInput.required = false;
            loginSubmitBtn.disabled = false;
            loginSubmitBtn.textContent = '📲 Get OTP on Telegram';
            loginErrorBox.classList.add('hidden');
        });
    }

    if (registerBackBtn) {
        registerBackBtn.addEventListener('click', (e) => {
            e.preventDefault();
            registerOtpGroup.classList.add('hidden');
            registerCredentialsGroup.classList.remove('hidden');
            registerOtpInput.required = false;
            registerSubmitBtn.disabled = false;
            registerSubmitBtn.textContent = '📲 Get OTP on Telegram';
            registerErrorBox.classList.add('hidden');
        });
    }
}

// ── Patient Session State Management (Gated UI Controller) ──
function loginPatient(patient) {
    if (!patient || !patient.patient_uid) return;

    currentPatientId = patient.patient_uid;
    currentPatientName = patient.name || 'Patient';

    // 1. Hide the entire full-screen Auth Overlay
    if (authOverlay) {
        authOverlay.classList.add('hidden');
    }

    // 2. Reveal Chat Area, Quick Action Chips, and Input Footer
    if (chatMessages) chatMessages.classList.remove('hidden');
    if (quickActions) quickActions.classList.remove('hidden');
    if (chatInputContainer) chatInputContainer.classList.remove('hidden');

    // 3. Update Top Navigation Bar Status Badge
    if (authLoggedOutContainer) authLoggedOutContainer.classList.add('hidden');
    if (authLoggedInContainer) authLoggedInContainer.classList.remove('hidden');
    if (authVerifiedText) {
        authVerifiedText.innerHTML = `<i class="fas fa-user-check" style="color: #10b981;"></i> <strong>${escapeHtml(currentPatientName)}</strong> <span style="font-size:0.75rem;color:#718096;">(${currentPatientId})</span>`;
    }

    // 4. Render initial greeting if chat is empty
    if (chatMessages && chatMessages.children.length === 0) {
        addWelcomeMessage();
    }
}

function logoutPatientUI() {
    currentPatientId = '';
    currentPatientName = '';

    // 1. Show the full-screen Auth Overlay Gate
    if (authOverlay) {
        authOverlay.classList.remove('hidden');
    }

    // 2. Hide Chat Area and Input
    if (chatMessages) chatMessages.classList.add('hidden');
    if (quickActions) quickActions.classList.add('hidden');
    if (chatInputContainer) chatInputContainer.classList.add('hidden');

    // 3. Reset Top Navigation Status Badge
    if (authLoggedInContainer) authLoggedInContainer.classList.add('hidden');
    if (authLoggedOutContainer) authLoggedOutContainer.classList.remove('hidden');

    // 4. Reset forms to Login view
    if (loginCard) loginCard.classList.remove('hidden');
    if (registerCard) registerCard.classList.add('hidden');
    if (profileSelectCard) profileSelectCard.classList.add('hidden');
    if (loginCredentialsGroup) loginCredentialsGroup.classList.remove('hidden');
    if (loginOtpGroup) loginOtpGroup.classList.add('hidden');
    if (loginSubmitBtn) {
        loginSubmitBtn.disabled = false;
        loginSubmitBtn.textContent = '📲 Get OTP on Telegram';
    }
}

async function logoutPatientSession() {
    try {
        await fetch(`${API_BASE}/api/patients/logout`, { method: 'POST' });
    } catch (err) {
        console.error("Logout error:", err);
    }
    logoutPatientUI();
    window.location.reload();
}
