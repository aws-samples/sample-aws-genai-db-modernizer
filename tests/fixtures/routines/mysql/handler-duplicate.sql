-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `twice_handled`()
BEGIN
  DECLARE CONTINUE HANDLER FOR SQLSTATE '23000' SET @dup = 1;
  DECLARE EXIT HANDLER FOR SQLSTATE VALUE '23000' SET @dup = 2;
  INSERT INTO orders (id) VALUES (1);
END
