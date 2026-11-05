-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

-- A procedure as a dump writes it, with its own delimiter.
DROP PROCEDURE IF EXISTS `refresh_totals`;
DELIMITER $$
CREATE DEFINER=`admin`@`%` PROCEDURE `refresh_totals`()
BEGIN
  UPDATE orders SET total = 0 WHERE total IS NULL;
  SELECT 'done; really $$ not a delimiter' AS msg;
END$$
DELIMITER ;
