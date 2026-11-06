-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `top_customers`(IN `rank` INT, IN p_since DATE)
    READS SQL DATA
BEGIN
  DECLARE `window` INT DEFAULT 7;
  DECLARE v_count INT DEFAULT 0;
  SET @row = 0;
  SET SESSION transaction_isolation = 'READ-COMMITTED';
  SELECT c.id, c.`rank`, COUNT(*) AS orders_placed, ST_AsText(c.location) AS location
    FROM customers c JOIN orders o ON o.customer_id = c.id
    WHERE o.created_at >= p_since - INTERVAL `window` DAY AND c.region = 'eu'
    GROUP BY c.id, c.`rank`, c.location
    ORDER BY orders_placed DESC
    LIMIT `rank`;
  SELECT COUNT(*) INTO v_count FROM (SELECT customer_id FROM orders GROUP BY customer_id) recent;
  INSERT INTO customer_totals (customer_id, total)
    SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id;
  SELECT GROUP_CONCAT(name ORDER BY name DESC) INTO @names FROM customers;
  SELECT ' rank, GROUP BY x DESC, PASSWORD(1) ' AS note;
END
