-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0

CREATE DEFINER=`admin`@`%` FUNCTION `legacy_token`(p_secret VARCHAR(64), p_place POINT) RETURNS VARCHAR(255)
    DETERMINISTIC
BEGIN
  DECLARE v_text VARCHAR(255);
  SET v_text = CONCAT(PASSWORD(p_secret), ENCRYPT(p_secret), AsText(p_place), X(p_place));
  RETURN v_text;
END
