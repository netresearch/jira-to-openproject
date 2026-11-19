// Copyright (c) 2026 Netresearch DTT GmbH
// SPDX-License-Identifier: MIT
'use strict';

const element = id => document.getElementById(id);
const number = new Intl.NumberFormat('en', {maximumFractionDigits: 1});
const timestamp = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'medium'
});
let snapshot = null;
let socket = null;
let heartbeat;
let reconnect;
let poll;
let disposed = false;
let version = 0;
let refreshing = false;

function formatMemory(mib) {
    if (!Number.isFinite(mib) || mib < 0) return '—';
    let value = mib;
    const units = ['MiB', 'GiB', 'TiB', 'PiB'];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return number.format(value) + '\u00a0' + units[unit];
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '';
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor(whole / 60) % 60;
    if (hours) return hours + 'h ' + minutes + 'm';
    if (minutes) return minutes + 'm ' + whole % 60 + 's';
    return whole + 's';
}

function formatTimestamp(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '' : timestamp.format(date);
}

function label(value) {
    const words = String(value ?? '').replaceAll('_', ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
}

function node(tag, text, css) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (css) result.className = css;
    return result;
}

function stat(term, value) {
    const row = node('div');
    row.append(node('dt', term), node('dd', value));
    return row;
}

function comparisonRow([name, pair]) {
    const row = node('tr');
    const heading = node('th', name);
    heading.scope = 'row';
    row.append(heading);
    for (const value of [pair.source, pair.target]) {
        row.append(Number.isInteger(value) ? node('td', number.format(value)) : node('td', 'Not checked', 'unmeasured'));
    }
    return row;
}

function renderComparison(data) {
    const select = element('comparison-scope');
    const projects = data.project_comparisons ?? {};
    const names = Object.keys(projects);
    const hasTotal = Object.keys(data.comparison_counts ?? {}).length > 0;
    const scopes = hasTotal ? ['', ...names] : names;
    const selected = select.value;
    // Preserve focus and selection when a live update only changes the counts.
    if (JSON.stringify(Array.from(select.options, option => option.value)) !== JSON.stringify(scopes)) {
        select.replaceChildren(...scopes.map(name => new Option(name || 'All projects', name)));
        if (scopes.includes(selected)) select.value = selected;
    }
    element('scope-control').hidden = names.length === 0;
    const rows = select.value ? projects[select.value] : data.comparison_counts;
    element('comparison-rows').replaceChildren(...Object.entries(rows ?? {}).map(comparisonRow));
    element('comparison-card').hidden = scopes.length === 0;
    return scopes.length > 0;
}

element('comparison-scope').addEventListener('change', () => {
    if (snapshot) renderComparison(snapshot);
});

function phaseRow(phase) {
    const item = node('li');
    const heading = node('div', undefined, 'phase-row');
    const status = node('span', label(phase.status), 'status');
    status.dataset.state = phase.status;
    heading.append(node('span', label(phase.name), 'phase-name'), status);
    item.append(heading);
    const times = [];
    if (phase.started_at) times.push('Started ' + formatTimestamp(phase.started_at));
    if (phase.finished_at) times.push('Finished ' + formatTimestamp(phase.finished_at));
    if (times.length) item.append(node('div', times.join(' · '), 'phase-times'));
    return item;
}

function showRows(id, children, emptyId) {
    element(id).replaceChildren(...children);
    element(emptyId).hidden = children.length > 0;
}

function renderProgressBar(data, phases) {
    let total = phases.length;
    let done = phases.filter(phase => phase.status === 'completed').length;
    let description = number.format(done) + ' of ' + number.format(total) + ' phases completed';
    if (Number.isInteger(data.total_entities) && data.total_entities > 0 && Number.isInteger(data.processed_entities)) {
        total = data.total_entities;
        done = data.processed_entities;
        description = number.format(done) + ' of ' + number.format(total) + ' entities processed';
    }
    const counts = data.verified_counts ?? {};
    const selected = counts[data.current_component + ' · Jira issues selected for worklog reads'];
    const checked = counts[data.current_component + ' · Jira issues with worklog IDs checked'];
    if (data.status === 'running' && Number.isInteger(selected) && selected > 0 &&
        Number.isInteger(checked) && checked >= 0 && checked <= selected) {
        total = selected;
        done = checked;
        description = number.format(done) + ' of ' + number.format(total) + ' Jira issues checked for worklogs';
    }
    element('progress-row').hidden = total === 0;
    if (!total) return;
    const bar = element('progress-bar');
    bar.max = total;
    bar.value = done;
    bar.setAttribute('aria-valuetext', description);
    element('progress-label').textContent = description;
}

function runDetails(data) {
    const times = [
        ['Started', formatTimestamp(data.start_time)],
        ['Last reported', formatTimestamp(data.last_update)],
        ['Finished', formatTimestamp(data.finished_at)],
        ['Elapsed', formatDuration(data.elapsed_seconds)]
    ];
    for (const [field, name] of [['total_entities', 'Total entities'], ['processed_entities', 'Processed'], ['failed_entities', 'Failed']]) {
        if (Number.isInteger(data[field])) times.push([name, number.format(data[field])]);
    }
    return times.filter(([,value]) => value !== '').map(([key,value]) => stat(key,value));
}

function renderProgress(data) {
    if (!data || typeof data.status !== 'string') return;
    if (snapshot && snapshot.migration_id !== data.migration_id) {
        element('events').replaceChildren();
        element('events-card').hidden = true;
    }
    snapshot = data;
    const idle = data.status === 'idle';
    const phases = data.phases ?? [];
    const status = element('run-status');
    status.textContent = label(data.status);
    status.dataset.state = data.status;
    element('migration-id').textContent = idle ? '' : data.migration_id;
    element('run-empty').textContent = 'No migration run has been reported.';
    element('run-empty').hidden = !idle;
    element('run-data').hidden = idle;
    element('active-phase').textContent = data.current_component ? label(data.current_component) : '';
    element('active-phase').hidden = !data.current_component;
    renderProgressBar(data, phases);
    element('run-times').replaceChildren(...runDetails(data));
    showRows('phases', phases.map(phaseRow), 'phases-empty');
    const hasComparisons = renderComparison(data);
    const counts = Object.entries(data.verified_counts ?? {});
    element('counts-card').hidden = hasComparisons && counts.length === 0;
    showRows('verified-counts', counts.map(([key,value]) => stat(label(key), number.format(value))), 'counts-empty');
    const checks = data.pending_checks ?? [];
    element('checks-empty').textContent = idle ? 'Awaiting check results.' : 'No pending checks reported.';
    showRows('checks', checks.map(check => node('li', label(check))), 'checks-empty');
    element('pilot-ram').textContent = formatMemory(data.memory_usage_mb);
    element('pilot-memory-row').hidden = !Number.isFinite(data.memory_usage_mb);
    element('export-progress').disabled = idle;
    const published = element('published-run-summary');
    if (published) published.hidden = true;
    element('live-dashboard').hidden = false;
}

function renderMetrics(data) {
    element('system-ram').textContent = formatMemory(data.memory_usage_mb);
    const cpu = data.cpu_usage_percent;
    element('system-cpu').textContent = Number.isFinite(cpu) ? number.format(cpu) + '%' : '—';
}

function renderEvent(data) {
    if (!data || typeof data.message !== 'string') return;
    const item = node('li');
    if (data.timestamp) {
        const time = node('time', formatTimestamp(data.timestamp));
        time.dateTime = data.timestamp;
        item.append(time);
    }
    item.append(node('span', data.message));
    const events = element('events');
    events.prepend(item);
    if (events.children.length > 25) events.lastElementChild.remove();
    element('events-card').hidden = false;
}

async function fetchJson(path) {
    const response = await fetch(path, {cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
}

async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const fetchVersion = version;
    try {
        const [progress, metrics] = await Promise.allSettled([fetchJson('/api/progress'), fetchJson('/api/metrics')]);
        const problems = [];
        if (progress.status === 'fulfilled') {
            if (version === fetchVersion) renderProgress(progress.value);
        } else {
            problems.push('Migration data could not be refreshed.');
            if (!snapshot) {
                if (!element('published-run-summary')) element('live-dashboard').hidden = false;
                element('run-status').textContent = 'Unavailable';
                element('run-empty').textContent = 'Waiting for the migration data source.';
            }
        }
        if (metrics.status === 'fulfilled') renderMetrics(metrics.value);
        else problems.push('System measurements could not be refreshed.');
        element('load-error').textContent = problems.join(' ');
        element('load-error').hidden = problems.length === 0;
    } finally {
        refreshing = false;
    }
}

function connect() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(scheme + '//' + location.host + '/ws/dashboard');
    const connection = element('connection');
    socket.onopen = async () => {
        connection.textContent = 'Live connection';
        connection.dataset.live = 'true';
        heartbeat = setInterval(() => {
            if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: 'heartbeat'}));
        }, 25000);
        await refresh();
    };
    socket.onmessage = event => {
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message.type === 'progress_update') {
            version++;
            renderProgress(message.data);
        }
        if (message.type === 'event') renderEvent(message.data);
    };
    socket.onclose = () => {
        clearInterval(heartbeat);
        connection.textContent = 'Periodic refresh';
        connection.dataset.live = 'false';
        if (!disposed) reconnect = setTimeout(connect, 5000);
    };
}

function download(content, filename, type) {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const link = node('a');
    link.download = filename;
    link.href = url;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

element('export-progress').addEventListener('click', () => {
    if (snapshot) download(JSON.stringify(snapshot, null, 2), 'migration-progress.json', 'application/json');
});
element('export-metrics').addEventListener('click', async () => {
    const error = element('export-error');
    error.textContent = '';
    try {
        const result = await fetchJson('/api/metrics/csv');
        download(result.csv_content, result.filename, 'text/csv');
    } catch {
        error.textContent = 'Metrics download failed. Please try again.';
    }
});
document.addEventListener('DOMContentLoaded', refresh, {once: true});
connect();
poll = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => {
    disposed = true;
    clearInterval(poll);
    clearInterval(heartbeat);
    clearTimeout(reconnect);
    socket?.close();
});
window.addEventListener('pageshow', event => {
    if (event.persisted) location.reload();
});
