-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `page_orders`(IN p_offset INT, OUT p_total INT)
BEGIN
  SELECT SQL_CALC_FOUND_ROWS id, total FROM orders ORDER BY id LIMIT p_offset, 20;
  SET p_total = FOUND_ROWS();
END
