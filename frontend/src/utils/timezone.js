/**
 * Timezone utility for AutoPod Cloud.
 * Converts UTC / ISO / SQLite timestamps into Indian Standard Time (IST, UTC+5:30)
 * and formats human-readable relative dates.
 */

export const formatIST = (timestamp) => {
  if (!timestamp) return '—';

  try {
    // If SQLite string format like "2026-11-05 14:02:11", treat as UTC
    let dateStr = timestamp;
    if (typeof timestamp === 'string') {
      const trimmed = timestamp.trim();
      if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(trimmed) && !trimmed.endsWith('Z')) {
        dateStr = trimmed.replace(' ', 'T') + 'Z';
      }
    }

    const d = new Date(dateStr);
    if (isNaN(d.getTime())) {
      return String(timestamp);
    }

    return new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(d) + ' IST';
  } catch (e) {
    return String(timestamp);
  }
};

export const formatISTShort = (timestamp) => {
  if (!timestamp) return '—';

  try {
    let dateStr = timestamp;
    if (typeof timestamp === 'string') {
      const trimmed = timestamp.trim();
      if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(trimmed) && !trimmed.endsWith('Z')) {
        dateStr = trimmed.replace(' ', 'T') + 'Z';
      }
    }

    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return String(timestamp);

    return new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(d) + ' IST';
  } catch (e) {
    return String(timestamp);
  }
};

export const timeAgo = (timestamp) => {
  if (!timestamp) return '—';

  try {
    let dateStr = timestamp;
    if (typeof timestamp === 'string') {
      const trimmed = timestamp.trim();
      if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(trimmed) && !trimmed.endsWith('Z')) {
        dateStr = trimmed.replace(' ', 'T') + 'Z';
      }
    }

    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return String(timestamp);

    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffSec = Math.floor(diffMs / 1000);

    if (diffSec < 5) return 'just now';
    if (diffSec < 60) return `${diffSec}s ago`;
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHour = Math.floor(diffMin / 60);
    if (diffHour < 24) return `${diffHour}h ago`;
    const diffDay = Math.floor(diffHour / 24);
    return `${diffDay}d ago`;
  } catch (e) {
    return '—';
  }
};

