import { ApiConfigurations } from '../config/GlobalConfigurations';
import { getMockResponse } from '../config/apiMockData';

class ApiManager {
    constructor(headers = {}) {
        this.customHeaders = headers;
        this.baseUrl = ApiConfigurations.baseUrl;
        this.timeout = ApiConfigurations.timeout;
        this.mode = ApiConfigurations.mode;
    }

    /**
     * Execute API calls (real or fake based on mode)
     * @param {Array} apiCalls - Array of API call objects with id, path, method, params
     * @returns {Object} Dictionary of responses keyed by id
     */
    async execute(apiCalls) {
        /*
        if (this.mode === 'fake') {
            return this.executeFake(apiCalls);
        }
        */
        return this.executeReal(apiCalls);
    }

    /**
     * Execute fake API calls using mock data
     * @param {Array} apiCalls - Array of API call objects
     * @returns {Object} Dictionary of mock responses keyed by id
     */
    async executeFake(apiCalls) {
        // Simulate network delay
        await new Promise(resolve => setTimeout(resolve, 300 + Math.random() * 700));

        const results = {};

        apiCalls.forEach(apiCall => {
            const mockData = getMockResponse(apiCall.method, apiCall.path);

            results[apiCall.id] = {
                id: apiCall.id,
                ...mockData,
                success: !mockData.error,
                status: mockData.error ? 404 : 200
            };
        });

        return results;
    }

    /**
     * Execute real API calls via XMLHttpRequest
     * @param {Array} apiCalls - Array of API call objects
     * @returns {Object} Dictionary of responses keyed by id
     */
    async executeReal(apiCalls) {
        try {
            // Get auth headers if not provided
            //const authHeaders = this.customHeaders || await SessionManager.getAuthHeaders();
            const authHeaders = this.customHeaders;
            console.log(authHeaders);
            // Execute all API calls in parallel
            const promises = apiCalls.map(apiCall =>
                new Promise((resolve) => {
                    const xhr = new XMLHttpRequest();

                    // Build full URL from baseUrl + path
                    // Remove leading slash from path if present to avoid double slashes
                    const path = apiCall.path.replace(/^\//, '');
                    const fullUrl = `${this.baseUrl}${path}`;

                    // Get HTTP method from apiCall (required property)
                    const method = apiCall.method.toUpperCase();

                    xhr.open(method, fullUrl, true);

                    // Set timeout
                    xhr.timeout = this.timeout;

                    // Set headers
                    Object.keys(authHeaders).forEach(key => {
                        xhr.setRequestHeader(key, authHeaders[key]);
                    });

                    // Set default headers from configuration
                    Object.keys(ApiConfigurations.headers).forEach(key => {
                        xhr.setRequestHeader(key, ApiConfigurations.headers[key]);
                    });

                    xhr.onload = function() {
                        if (xhr.status >= 200 && xhr.status < 300) {
                            try {
                                console.log(xhr.responseText);
                                const response = xhr.responseText ? JSON.parse(xhr.responseText) : {};
                                resolve({ id: apiCall.id, ...response, success: true, status: xhr.status });
                            } catch (parseError) {
                                resolve({ id: apiCall.id, error: parseError, success: false, status: xhr.status });
                            }
                        } else {
                            resolve({
                                id: apiCall.id,
                                error: new Error(`HTTP ${xhr.status}: ${xhr.statusText}`),
                                success: false,
                                status: xhr.status
                            });
                        }
                    };

                    xhr.onerror = function() {
                        resolve({ id: apiCall.id, error: new Error('Network error'), success: false });
                    };

                    xhr.ontimeout = function() {
                        resolve({ id: apiCall.id, error: new Error('Request timeout'), success: false });
                    };

                    // Send request with params (if any)
                    if (method === 'GET' || method === 'DELETE') {
                        // For GET/DELETE, params are typically in URL query string
                        xhr.send();
                    } else {
                        // For POST/PUT, send params in body
                        xhr.send(JSON.stringify(apiCall.params || {}));
                    }
                })
            );

            const responses = await Promise.all(promises);

            // Convert array to dictionary keyed by id
            const results = {};
            responses.forEach(response => {
                results[response.id] = response;
            });

            return results;
        } catch (error) {
            console.error('APIBatch execution error:', error);
            throw error;
        }
    }
}

export default ApiManager;
