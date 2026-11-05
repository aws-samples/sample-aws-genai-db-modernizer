-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `undeclared_condition`()
BEGIN
  DECLARE CONTINUE HANDLER FOR missing_row SET @failed = 1;
  DELETE FROM orders WHERE id = 0;
END
