-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `late_declare`()
BEGIN
  SELECT 1;
  DECLARE v_id INT;
END
