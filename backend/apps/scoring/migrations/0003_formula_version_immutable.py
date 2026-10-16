"""A formula version that a score run has used cannot be changed or deleted.

Only ``notes`` and ``code_ref`` may still change. To change scoring, publish a new version.
"""

from django.db import migrations

FUNCTION = """
CREATE FUNCTION scoring_formulaversion_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM scoring_scorerun WHERE formula_version_id = OLD.id) THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'formula version % is used by a score run and cannot be deleted', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.name IS DISTINCT FROM OLD.name
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.config IS DISTINCT FROM OLD.config
       OR NEW.quirks IS DISTINCT FROM OLD.quirks
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from THEN
        RAISE EXCEPTION
            'formula version % is used by a score run and cannot be changed; create a new version',
            OLD.id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
"""

TRIGGER = """
CREATE TRIGGER scoring_formulaversion_immutable
BEFORE UPDATE OR DELETE ON scoring_formulaversion
FOR EACH ROW EXECUTE FUNCTION scoring_formulaversion_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("scoring", "0002_seed_parity_formula")]

    operations = [
        migrations.RunSQL(FUNCTION, "DROP FUNCTION IF EXISTS scoring_formulaversion_guard() CASCADE"),
        migrations.RunSQL(
            TRIGGER, "DROP TRIGGER IF EXISTS scoring_formulaversion_immutable ON scoring_formulaversion"
        ),
    ]
