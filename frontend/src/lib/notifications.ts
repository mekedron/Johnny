/**
 * Browser notification permission + service worker bootstrap (US-027).
 *
 * The service worker is registered once per session on the first call to
 * :func:`bootstrapNotifications` (idempotent). It owns the notification
 * lifecycle: when the layout sees an ``approval_pending`` WebSocket
 * event, it asks the worker to ``showNotification`` with Approve/Reject
 * actions; the worker handles the click and posts back to the backend
 * via ``fetch``.
 *
 * Permission is requested at most once per session — if the user has
 * already denied, we don't nag them; the in-UI approval panel is the
 * authoritative experience and notifications are an enhancement.
 *
 * The two helpers used elsewhere:
 *  - :func:`bootstrapNotifications` — call on layout mount.
 *  - :func:`showApprovalNotification` — call when an approval_pending
 *    event arrives so the worker raises a system notification.
 *
 * All functions are no-ops in SSR (when ``window`` is undefined) so the
 * SvelteKit build doesn't break.
 */

const SESSION_FLAG_KEY = 'johnny.notification-permission-requested';

export interface ApprovalNotificationPayload {
	botSessionId: number;
	decisionId: number;
	suggestedReply: string;
	reason?: string;
	timeoutS: number;
}

export interface ReloginNotificationPayload {
	accountId: number;
	accountEmail: string;
	message: string;
}

export type NotificationPermissionLike =
	| 'default'
	| 'granted'
	| 'denied'
	| 'unsupported';

let registrationPromise: Promise<ServiceWorkerRegistration | null> | null = null;

function isBrowser(): boolean {
	return typeof window !== 'undefined';
}

function notificationsSupported(): boolean {
	if (!isBrowser()) return false;
	return (
		'Notification' in window &&
		'serviceWorker' in navigator
	);
}

function alreadyRequestedThisSession(): boolean {
	if (!isBrowser()) return false;
	try {
		return sessionStorage.getItem(SESSION_FLAG_KEY) === 'true';
	} catch {
		return false;
	}
}

function markRequested(): void {
	if (!isBrowser()) return;
	try {
		sessionStorage.setItem(SESSION_FLAG_KEY, 'true');
	} catch {
		// Storage may be blocked (private mode). Worst case we re-ask once.
	}
}

/**
 * Register the service worker (once) and, when appropriate, request
 * notification permission. Returns the current permission level so the
 * caller can render UI cues.
 */
export async function bootstrapNotifications(): Promise<NotificationPermissionLike> {
	if (!notificationsSupported()) return 'unsupported';

	if (registrationPromise === null) {
		registrationPromise = navigator.serviceWorker
			.register('/service-worker.js', { type: 'classic' })
			.then((reg) => reg)
			.catch((err) => {
				// Surfacing in console — the user lands on the same
				// in-UI approval feed even when SW registration fails.
				console.warn('johnny: service worker registration failed', err);
				return null;
			});
	}
	await registrationPromise;

	const current = Notification.permission;
	if (current !== 'default') {
		return current;
	}
	if (alreadyRequestedThisSession()) {
		return current;
	}
	try {
		markRequested();
		const result = await Notification.requestPermission();
		return result;
	} catch (err) {
		console.warn('johnny: notification permission request failed', err);
		return 'default';
	}
}

/**
 * Surface a system notification for a pending approval. The service
 * worker handles the Approve/Reject action buttons.
 *
 * Falls back to ``new Notification(...)`` directly in the page when the
 * service worker isn't available — covers the case where SW registration
 * was blocked but permission is still granted.
 */
export async function showApprovalNotification(
	payload: ApprovalNotificationPayload
): Promise<void> {
	if (!notificationsSupported()) return;
	if (Notification.permission !== 'granted') return;

	const tag = `johnny:approval:${payload.decisionId}`;
	const title = 'Johnny wants to speak';
	const body = payload.suggestedReply
		? `Suggested reply: "${payload.suggestedReply}"`
		: 'Approve to let Johnny respond.';

	const data = {
		kind: 'approval',
		botSessionId: payload.botSessionId,
		decisionId: payload.decisionId,
		suggestedReply: payload.suggestedReply,
		reason: payload.reason ?? '',
		timeoutS: payload.timeoutS
	};

	try {
		const reg = await navigator.serviceWorker.ready;
		await reg.showNotification(title, {
			body,
			tag,
			requireInteraction: true,
			data
		} as NotificationOptions);
	} catch (err) {
		console.warn('johnny: showNotification via SW failed; falling back', err);
		// Fallback path — no actions, but at least the user sees the alert.
		try {
			const fallback = new Notification(title, { body, tag, data });
			fallback.onclick = () => {
				window.focus();
				fallback.close();
			};
		} catch (err2) {
			console.warn('johnny: in-page Notification fallback failed', err2);
		}
	}
}

/**
 * Surface a system notification telling the operator a bot account is
 * signed out and needs re-login (Johnny-ebf). The service worker handles
 * the click: it deep-links to ``/settings?relogin=<accountId>`` which opens
 * that account's sign-in directly — one click from alert to fix.
 *
 * Falls back to an in-page ``Notification`` (focuses the window on click)
 * when the service worker isn't available.
 */
export async function showReloginNotification(
	payload: ReloginNotificationPayload
): Promise<void> {
	if (!notificationsSupported()) return;
	if (Notification.permission !== 'granted') return;

	const tag = `johnny:relogin:${payload.accountId}`;
	const title = 'Bot account signed out';
	const body = payload.message;
	const data = {
		kind: 'relogin',
		accountId: payload.accountId,
		accountEmail: payload.accountEmail
	};

	try {
		const reg = await navigator.serviceWorker.ready;
		await reg.showNotification(title, {
			body,
			tag,
			requireInteraction: true,
			actions: [{ action: 'relogin', title: 'Log in again' }],
			data
		} as NotificationOptions);
	} catch (err) {
		console.warn('johnny: relogin showNotification via SW failed; falling back', err);
		try {
			const fallback = new Notification(title, { body, tag, data });
			fallback.onclick = () => {
				window.open(`/settings?relogin=${payload.accountId}`, '_blank');
				fallback.close();
			};
		} catch (err2) {
			console.warn('johnny: in-page relogin Notification fallback failed', err2);
		}
	}
}

/**
 * Cancel a previously-shown approval notification (e.g. when the user
 * already approved/rejected from inside the page, or the timeout fired).
 */
export async function clearApprovalNotification(decisionId: number): Promise<void> {
	if (!notificationsSupported()) return;
	if (!('serviceWorker' in navigator)) return;
	try {
		const reg = await navigator.serviceWorker.ready;
		const notes = await reg.getNotifications({
			tag: `johnny:approval:${decisionId}`
		});
		for (const note of notes) {
			note.close();
		}
	} catch (err) {
		console.warn('johnny: clearApprovalNotification failed', err);
	}
}

// Test-only helper for the unit suite.
export function _resetForTesting(): void {
	registrationPromise = null;
}
