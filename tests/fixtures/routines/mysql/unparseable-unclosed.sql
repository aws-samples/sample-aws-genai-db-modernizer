-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `cut_off`()
BEGIN
  IF (SELECT COUNT(*) FROM orders) > 0 THEN
    UPDATE orders SET total = 0;
END
