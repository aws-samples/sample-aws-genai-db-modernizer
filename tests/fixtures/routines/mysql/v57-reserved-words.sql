-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `rank_orders`(IN rank INT)
BEGIN
  DECLARE lag INT DEFAULT 0;
  groups: LOOP
    LEAVE groups;
  END LOOP groups;
  SELECT o.id, o.rank FROM orders o WHERE o.rank <= rank ORDER BY o.id;
  UPDATE orders SET system = 'web' WHERE id = lag;
END
