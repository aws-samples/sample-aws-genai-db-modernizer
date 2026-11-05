-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `export_orders`()
BEGIN
  SELECT id, total INTO OUTFILE '/var/lib/mysql-files/orders.csv' FIELDS TERMINATED BY ',' FROM orders;
END
