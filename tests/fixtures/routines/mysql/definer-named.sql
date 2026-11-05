-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`legacy_ops`@`10.0.%` PROCEDURE `nightly_rollup`()
BEGIN
  INSERT INTO daily_totals (day, total) SELECT DATE(created_at), SUM(total) FROM orders GROUP BY DATE(created_at);
END
