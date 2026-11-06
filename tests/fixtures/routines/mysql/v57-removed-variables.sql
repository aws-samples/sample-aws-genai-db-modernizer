-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` EVENT `nightly_rollup` ON SCHEDULE EVERY 1 DAY DO BEGIN
  SET SESSION tx_isolation = 'READ-COMMITTED';
  IF @@session.tx_read_only = 0 THEN
    DELETE FROM app_logs WHERE created_at < NOW() - INTERVAL 30 DAY;
  END IF;
END
