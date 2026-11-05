-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=CURRENT_USER FUNCTION `order_tax`(p_id INT) RETURNS decimal(10,2) CHARSET utf8mb4
    READS SQL DATA
    DETERMINISTIC
RETURN (SELECT total * 0.2 FROM orders WHERE id = p_id)
