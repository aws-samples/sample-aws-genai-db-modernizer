/**
 * SessionManager - Simplified version without authentication
 * Kept for compatibility with existing code
 */

class SessionManager {
    static async getToken() {
        return null;
    }

    static async getAccessToken() {
        return null;
    }

    static async getRefreshToken() {
        return null;
    }

    static async getUser() {
        return null;
    }

    static async getUserIdentifier() {
        return null;
    }

    static async getUserProfile() {
        return null;
    }

    static async getValue(keyName) {
        return null;
    }

    static async isValidSession() {
        return true; // Always valid since no auth
    }

    static clearSession() {
        // No-op
    }

    static async getAuthHeaders() {
        return {};
    }

    static extendSession() {
        return true;
    }
}

export default SessionManager;
