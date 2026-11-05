-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` PROCEDURE `export_orders_s3`()
BEGIN
  SELECT id, total INTO OUTFILE S3 's3://example-bucket/exports/orders' FIELDS TERMINATED BY ',' FROM orders;
END
