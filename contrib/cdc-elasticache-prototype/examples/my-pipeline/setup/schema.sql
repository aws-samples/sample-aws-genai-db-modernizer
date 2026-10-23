-- CDC-cache sample workload — canonical schema
-- Matches the tables declared in ../config.yaml so the example runs end to end.
--
-- Relationships (used by the denormalization transform):
--   users.id              <- customer_kyc.user_id
--   customer_kyc.customer_id <- customer_txn_aggregates.customer_id
--
-- The denormalize rule in transforms/customer_kyc.yaml merges a customer's
-- transaction aggregates into the customer_kyc:<customer_id> cache object.

-- Note: the target database is created/selected by seed.py from SOURCE_DB_NAME.
-- If running this file standalone, prefix with: CREATE DATABASE IF NOT EXISTS <db>; USE <db>;

-- Application users (straight sync)
CREATE TABLE IF NOT EXISTS users (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    email       VARCHAR(320) NOT NULL,
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Merchants (straight sync) — hot reference data
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    category    VARCHAR(50),
    country     VARCHAR(5),
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',
    risk_tier   VARCHAR(20)  NOT NULL DEFAULT 'low',
    fee_rate    DECIMAL(5,4) NOT NULL DEFAULT 0.0275,
    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Product catalog (row-filtered: only status='active' is cached)
CREATE TABLE IF NOT EXISTS products (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    sku         VARCHAR(50)  NOT NULL,
    name        VARCHAR(300) NOT NULL,
    category    VARCHAR(100),
    price       DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',
    stock_count INT          NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Customer KYC (denormalization PARENT — txn aggregates + computed risk merge here)
CREATE TABLE IF NOT EXISTS customer_kyc (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT          NOT NULL,
    full_name   VARCHAR(200) NOT NULL,
    country     VARCHAR(5),
    kyc_status  VARCHAR(20)  NOT NULL DEFAULT 'verified',
    updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_user (user_id)
);

-- Customer transaction aggregates (denormalization CHILD — one row per customer/period)
CREATE TABLE IF NOT EXISTS customer_txn_aggregates (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    customer_id        INT          NOT NULL,
    period             VARCHAR(10)  NOT NULL,
    txn_count          INT          NOT NULL DEFAULT 0,
    total_amount       DECIMAL(14,2) NOT NULL DEFAULT 0.00,
    avg_txn_amount     DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    max_single_txn     DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    failed_txn_count   INT          NOT NULL DEFAULT 0,
    cross_border_count INT          NOT NULL DEFAULT 0,
    updated_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_customer (customer_id)
);
