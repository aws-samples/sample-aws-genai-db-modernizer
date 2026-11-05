-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `bad_order`()
BEGIN
  DECLARE c1 CURSOR FOR SELECT id FROM orders;
  DECLARE v_id INT;
  OPEN c1;
END
