-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `region_report`()
BEGIN
  DECLARE v_done INT DEFAULT 0;
  DECLARE v_region VARCHAR(16);
  DECLARE c_regions CURSOR FOR SELECT region FROM customers GROUP BY region;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;
  OPEN c_regions;
  CLOSE c_regions;
  SELECT region, COUNT(*) FROM customers GROUP BY region DESC;
END
