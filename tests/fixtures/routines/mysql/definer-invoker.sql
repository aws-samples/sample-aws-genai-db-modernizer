-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`legacy_ops`@`10.0.%` PROCEDURE `report_orders`()
    SQL SECURITY INVOKER
SELECT id, total FROM orders
