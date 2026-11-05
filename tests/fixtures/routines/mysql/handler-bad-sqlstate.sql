-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `bad_sqlstate`()
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '00000' SET @ok = 1;
  SELECT 1;
END
