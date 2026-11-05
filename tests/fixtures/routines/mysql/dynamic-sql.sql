-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `count_rows`(IN p_table VARCHAR(64))
BEGIN
  SET @q = CONCAT('SELECT COUNT(*) FROM ', p_table);
  PREPARE stmt FROM @q;
  EXECUTE stmt;
  DEALLOCATE PREPARE stmt;
END
