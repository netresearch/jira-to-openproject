// Dashboard JavaScript with Vue.js 3 and enhanced WebSocket support

const { createApp, ref, reactive, onMounted, onUnmounted, computed, watch } = Vue;

const dashboardApp = createApp({
    setup() {
        // Reactive data
        const progress = reactive({
            migration_id: null,
            total_entities: 0,
            processed_entities: 0,
            failed_entities: 0,
            current_entity: null,
            current_entity_type: null,
            current_component: null,
            status: 'idle', // 'idle', 'running', 'completed', 'failed', 'paused'
            start_time: null,
            last_update: null,
            error_count: 0,
            success_rate: 0.0,
            estimated_time_remaining: null,
            pause_time: null,
            total_pause_time: 0
        });

        const metrics = reactive({
            migration_id: null,
            entities_per_second: 0.0,
            average_processing_time: 0.0,
            memory_usage_mb: 0.0,
            cpu_usage_percent: 0.0,
            network_requests_per_second: 0.0,
            error_rate: 0.0,
            throughput_history: []
        });

        const recentEvents = ref([]);
        const connectionStatus = ref(false);
        const websocket = ref(null);
        const charts = reactive({});
        const heartbeatInterval = ref(null);
        const reconnectAttempts = ref(0);
        const maxReconnectAttempts = 5;

        // Computed properties
        const progressPercentage = computed(() => {
            if (progress.total_entities === 0) return 0;
            return Math.round((progress.processed_entities / progress.total_entities) * 100);
        });

        const elapsedTime = computed(() => {
            if (!progress.start_time) return '00:00:00';
            const start = new Date(progress.start_time);
            const now = new Date();
            const elapsed = now - start - (progress.total_pause_time * 1000);
            return formatTime(Math.max(0, elapsed));
        });

        const statusClass = computed(() => {
            switch (progress.status) {
                case 'running': return 'text-success';
                case 'completed': return 'text-success';
                case 'failed': return 'text-danger';
                case 'paused': return 'text-warning';
                default: return 'text-muted';
            }
        });

        const canStart = computed(() => progress.status === 'idle' || progress.status === 'failed');
        const canStop = computed(() => progress.status === 'running');
        const canPause = computed(() => progress.status === 'running');
        const canResume = computed(() => progress.status === 'paused');

        // Methods
        const connectWebSocket = () => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws/dashboard`;

            websocket.value = new WebSocket(wsUrl);
            connectionStatus.value = false;

            websocket.value.onopen = () => {
                connectionStatus.value = true;
                reconnectAttempts.value = 0;
                console.log('WebSocket connected');

                // Start heartbeat
                startHeartbeat();

                // Request initial status
                sendWebSocketMessage({
                    type: 'request_status'
                });
            };

            websocket.value.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    handleWebSocketMessage(data);
                } catch (error) {
                    console.error('Error parsing WebSocket message:', error);
                }
            };

            websocket.value.onclose = () => {
                connectionStatus.value = false;
                console.log('WebSocket disconnected');
                stopHeartbeat();

                // Attempt to reconnect
                if (reconnectAttempts.value < maxReconnectAttempts) {
                    reconnectAttempts.value++;
                    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts.value), 30000);
                    setTimeout(connectWebSocket, delay);
                }
            };

            websocket.value.onerror = (error) => {
                connectionStatus.value = false;
                console.error('WebSocket error:', error);
            };
        };

        const startHeartbeat = () => {
            heartbeatInterval.value = setInterval(() => {
                if (websocket.value && websocket.value.readyState === WebSocket.OPEN) {
                    sendWebSocketMessage({
                        type: 'heartbeat',
                        timestamp: new Date().toISOString()
                    });
                }
            }, 30000); // Send heartbeat every 30 seconds
        };

        const stopHeartbeat = () => {
            if (heartbeatInterval.value) {
                clearInterval(heartbeatInterval.value);
                heartbeatInterval.value = null;
            }
        };

        const sendWebSocketMessage = (message) => {
            if (websocket.value && websocket.value.readyState === WebSocket.OPEN) {
                websocket.value.send(JSON.stringify(message));
            }
        };

        const handleWebSocketMessage = (data) => {
            switch (data.type) {
                case 'connection_established':
                    // Connection established - no logging needed (prevents log injection)
                    break;

                case 'progress_update':
                    Object.assign(progress, data.data);
                    updateProgressChart();
                    break;

                case 'metrics_update':
                    Object.assign(metrics, data.data);
                    updateMetricsCharts();
                    break;

                case 'event':
                    addEvent(data.data);
                    break;

                case 'status_update':
                    updateStatus(data.data);
                    break;

                case 'heartbeat_response':
                    // Heartbeat acknowledged
                    break;

                case 'error':
                    addEvent({
                        level: 'error',
                        message: data.message,
                        timestamp: new Date().toISOString()
                    });
                    break;

                default:
                    // Unknown message type - silently ignore (prevents log injection)
                    break;
            }
        };

        const addEvent = (eventData) => {
            const event = {
                timestamp: new Date(eventData.timestamp || Date.now()),
                level: eventData.level || 'info',
                message: eventData.message,
                entity: eventData.entity,
                component: eventData.component,
                details: eventData.details
            };

            recentEvents.value.unshift(event);

            // Keep only last 50 events
            if (recentEvents.value.length > 50) {
                recentEvents.value = recentEvents.value.slice(0, 50);
            }

            // Update charts if needed
            if (event.level === 'error') {
                progress.error_count++;
            }
        };

        const updateStatus = (statusData) => {
            // Update progress with status data
            if (statusData.is_running !== undefined) {
                progress.status = statusData.is_running ? 'running' : 'idle';
            }
            if (statusData.migration_id) {
                progress.migration_id = statusData.migration_id;
            }
            if (statusData.current_component) {
                progress.current_component = statusData.current_component;
            }
            if (statusData.start_time) {
                progress.start_time = statusData.start_time;
            }
            if (statusData.pause_time) {
                progress.pause_time = statusData.pause_time;
            }
            if (statusData.total_pause_time !== undefined) {
                progress.total_pause_time = statusData.total_pause_time;
            }
        };

        const updateProgressChart = () => {
            if (charts.progress) {
                const processed = progress.processed_entities;
                const remaining = progress.total_entities - progress.processed_entities;

                charts.progress.data.datasets[0].data = [processed, remaining];
                charts.progress.update('none'); // Update without animation for performance
            }
        };

        const updateMetricsCharts = () => {
            if (charts.throughput) {
                // Add new data point to throughput chart
                const now = new Date();
                charts.throughput.data.labels.push(now.toLocaleTimeString());
                charts.throughput.data.datasets[0].data.push(metrics.entities_per_second);

                // Keep only last 20 data points
                if (charts.throughput.data.labels.length > 20) {
                    charts.throughput.data.labels.shift();
                    charts.throughput.data.datasets[0].data.shift();
                }

                charts.throughput.update('none');
            }
        };

        const initializeCharts = () => {
            // Progress Chart
            const progressCtx = document.getElementById('processingRateChart');
            if (progressCtx) {
                charts.progress = new Chart(progressCtx, {
                    type: 'doughnut',
                    data: {
                        labels: ['Processed', 'Remaining'],
                        datasets: [{
                            data: [0, 100],
                            backgroundColor: ['#28a745', '#e9ecef'],
                            borderWidth: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                display: false
                            }
                        }
                    }
                });
            }

            // Entity Distribution Chart
            const distributionCtx = document.getElementById('entityDistributionChart');
            if (distributionCtx) {
                charts.distribution = new Chart(distributionCtx, {
                    type: 'pie',
                    data: {
                        labels: ['Issues', 'Projects', 'Users', 'Comments', 'Attachments'],
                        datasets: [{
                            data: [0, 0, 0, 0, 0],
                            backgroundColor: [
                                '#007bff',
                                '#28a745',
                                '#ffc107',
                                '#17a2b8',
                                '#6f42c1'
                            ],
                            borderWidth: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                position: 'bottom'
                            }
                        }
                    }
                });
            }

            // Throughput Chart
            const throughputCtx = document.getElementById('throughputChart');
            if (throughputCtx) {
                charts.throughput = new Chart(throughputCtx, {
                    type: 'line',
                    data: {
                        labels: [],
                        datasets: [{
                            label: 'Entities per Second',
                            data: [],
                            borderColor: '#007bff',
                            backgroundColor: 'rgba(0, 123, 255, 0.1)',
                            tension: 0.4,
                            fill: true
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                display: false
                            }
                        },
                        scales: {
                            y: {
                                beginAtZero: true
                            }
                        }
                    }
                });
            }
        };

        const formatTime = (milliseconds) => {
            const seconds = Math.floor(milliseconds / 1000);
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const secs = seconds % 60;
            return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        };

        const startMigration = async () => {
            try {
                const response = await fetch('/api/migration/start', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        action: 'start',
                        components: ['users', 'projects', 'workpackages'],
                        config: {
                            component: 'all',
                            batch_size: 100
                        }
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    console.log('Migration started:', result);
                    addEvent({
                        level: 'info',
                        message: 'Migration started successfully',
                        timestamp: new Date().toISOString()
                    });
                } else {
                    const error = await response.json();
                    throw new Error(error.detail || 'Failed to start migration');
                }
            } catch (error) {
                console.error('Error starting migration:', error);
                addEvent({
                    level: 'error',
                    message: `Failed to start migration: ${error.message}`,
                    timestamp: new Date().toISOString()
                });
            }
        };

        const stopMigration = async () => {
            try {
                const response = await fetch('/api/migration/stop', {
                    method: 'POST'
                });

                if (response.ok) {
                    const result = await response.json();
                    console.log('Migration stopped:', result);
                    addEvent({
                        level: 'info',
                        message: 'Migration stop requested',
                        timestamp: new Date().toISOString()
                    });
                } else {
                    const error = await response.json();
                    throw new Error(error.detail || 'Failed to stop migration');
                }
            } catch (error) {
                console.error('Error stopping migration:', error);
                addEvent({
                    level: 'error',
                    message: `Failed to stop migration: ${error.message}`,
                    timestamp: new Date().toISOString()
                });
            }
        };

        const pauseMigration = async () => {
            try {
                const response = await fetch('/api/migration/pause', {
                    method: 'POST'
                });

                if (response.ok) {
                    const result = await response.json();
                    console.log('Migration paused:', result);
                    addEvent({
                        level: 'info',
                        message: 'Migration pause requested',
                        timestamp: new Date().toISOString()
                    });
                } else {
                    const error = await response.json();
                    throw new Error(error.detail || 'Failed to pause migration');
                }
            } catch (error) {
                console.error('Error pausing migration:', error);
                addEvent({
                    level: 'error',
                    message: `Failed to pause migration: ${error.message}`,
                    timestamp: new Date().toISOString()
                });
            }
        };

        const resumeMigration = async () => {
            try {
                const response = await fetch('/api/migration/resume', {
                    method: 'POST'
                });

                if (response.ok) {
                    const result = await response.json();
                    console.log('Migration resumed:', result);
                    addEvent({
                        level: 'info',
                        message: 'Migration resume requested',
                        timestamp: new Date().toISOString()
                    });
                } else {
                    const error = await response.json();
                    throw new Error(error.detail || 'Failed to resume migration');
                }
            } catch (error) {
                console.error('Error resuming migration:', error);
                addEvent({
                    level: 'error',
                    message: `Failed to resume migration: ${error.message}`,
                    timestamp: new Date().toISOString()
                });
            }
        };

        const exportMetrics = async () => {
            try {
                const response = await fetch('/api/metrics/csv');
                if (response.ok) {
                    const data = await response.json();

                    // Create and download CSV file
                    const blob = new Blob([data.csv_content], { type: 'text/csv' });
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = data.filename;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    window.URL.revokeObjectURL(url);

                    addEvent({
                        level: 'info',
                        message: 'Metrics exported successfully',
                        timestamp: new Date().toISOString()
                    });
                }
            } catch (error) {
                console.error('Error exporting metrics:', error);
                addEvent({
                    level: 'error',
                    message: 'Failed to export metrics',
                    timestamp: new Date().toISOString()
                });
            }
        };

        const exportProgress = async () => {
            try {
                const response = await fetch('/api/progress');
                if (response.ok) {
                    const data = await response.json();

                    // Create and download JSON file
                    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `migration_progress_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.json`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    window.URL.revokeObjectURL(url);

                    addEvent({
                        level: 'info',
                        message: 'Progress exported successfully',
                        timestamp: new Date().toISOString()
                    });
                }
            } catch (error) {
                console.error('Error exporting progress:', error);
                addEvent({
                    level: 'error',
                    message: 'Failed to export progress',
                    timestamp: new Date().toISOString()
                });
            }
        };

        const clearEvents = () => {
            recentEvents.value = [];
        };

        // Lifecycle hooks
        onMounted(() => {
            connectWebSocket();
            initializeCharts();

            // Load initial data
            fetch('/api/migration/status')
                .then(response => response.json())
                .then(data => {
                    updateStatus(data);
                })
                .catch(error => {
                    console.error('Error loading migration status:', error);
                });

            fetch('/api/metrics')
                .then(response => response.json())
                .then(data => {
                    Object.assign(metrics, data);
                })
                .catch(error => {
                    console.error('Error loading metrics:', error);
                });
        });

        onUnmounted(() => {
            stopHeartbeat();
            if (websocket.value) {
                websocket.value.close();
            }
        });

        return {
            progress,
            metrics,
            recentEvents,
            connectionStatus,
            progressPercentage,
            elapsedTime,
            statusClass,
            canStart,
            canStop,
            canPause,
            canResume,
            startMigration,
            stopMigration,
            pauseMigration,
            resumeMigration,
            exportMetrics,
            exportProgress,
            clearEvents,
            formatTime
        };
    }
});

// Mount the app when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    dashboardApp.mount('#app');
});
