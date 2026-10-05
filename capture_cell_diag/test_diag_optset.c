/* test_diag_optset.c - standalone unit tests for the runtime option parse.
 *
 * Links only diag_optset.c (libc, no Kismet), so it runs off the capture
 * framework: `make check` in this directory.
 *
 * What is worth testing here is the CLASSIFICATION, not the string splitting.
 * `mask=full` and `mask=banana` and `masc=full` must land in three different
 * buckets, because the operator's next action differs for each -- reopen the
 * source, fix the value, fix the spelling. The failure mode this guards is a
 * knob that answers "ok" and changes nothing.
 */

#include "diag_optset.h"

#include <stdio.h>
#include <string.h>

static int failures = 0;

#define CHECK(cond, ...) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
        fprintf(stderr, __VA_ARGS__); \
        fprintf(stderr, "\n"); \
        failures++; \
    } \
} while (0)

static diag_optset_t P(const char *spec) {
    diag_optset_t o;
    diag_optset_parse(spec, &o);
    return o;
}

/* ── the settable knob ─────────────────────────────────────────────────── */
static void test_rawlog_path_is_settable(void) {
    diag_optset_t o = P("rawlog=/captures/run.hdlc");
    CHECK(o.status == DIAG_OPTSET_OK, "status %d", o.status);
    CHECK(o.key == DIAG_OPTSET_KEY_RAWLOG, "key %d", o.key);
    CHECK(strcmp(o.value, "/captures/run.hdlc") == 0, "value '%s'", o.value);
    CHECK(o.rawlog_disable == 0, "a path must not read as a disable");
    CHECK(o.err[0] == '\0', "OK carried an error: '%s'", o.err);
}

static void test_rawlog_off_and_none_both_disable(void) {
    diag_optset_t off  = P("rawlog=off");
    diag_optset_t none = P("rawlog=none");
    CHECK(off.status == DIAG_OPTSET_OK && off.rawlog_disable == 1,
          "rawlog=off status %d disable %d", off.status, off.rawlog_disable);
    /* The same two spellings f3= accepts. An operator should not have to
     * remember which option spells "not armed" which way. */
    CHECK(none.status == DIAG_OPTSET_OK && none.rawlog_disable == 1,
          "rawlog=none status %d disable %d", none.status, none.rawlog_disable);
}

static void test_a_path_named_offering_is_not_a_disable(void) {
    /* Prefix matching would make this a disable and silently stop the tee. */
    diag_optset_t o = P("rawlog=/captures/offering.hdlc");
    CHECK(o.status == DIAG_OPTSET_OK, "status %d", o.status);
    CHECK(o.rawlog_disable == 0, "'offering' matched as 'off'");
}

static void test_surrounding_whitespace_is_trimmed(void) {
    /* A knob typed into a web form arrives with a newline more often than not;
     * failing it as an unknown value would misreport what was typed. */
    diag_optset_t o = P("rawlog= off\n");
    CHECK(o.status == DIAG_OPTSET_OK, "status %d (err '%s')", o.status, o.err);
    CHECK(o.rawlog_disable == 1, "trimmed value did not read as a disable");
}

/* ── the declined knobs: recognised, and NOT settable ──────────────────── */
static void test_mask_and_f3_are_declined_not_rejected(void) {
    diag_optset_t m = P("mask=full");
    diag_optset_t f = P("f3=high");

    CHECK(m.status == DIAG_OPTSET_OPEN_TIME_ONLY, "mask status %d", m.status);
    CHECK(m.key == DIAG_OPTSET_KEY_MASK, "mask key %d", m.key);
    CHECK(f.status == DIAG_OPTSET_OPEN_TIME_ONLY, "f3 status %d", f.status);
    CHECK(f.key == DIAG_OPTSET_KEY_F3, "f3 key %d", f.key);

    /* The message must name the way FORWARD, not just the refusal. Kismet
     * ships close_source/open_source; an operator told only "no" will read the
     * knob as broken rather than as pointing at another door. */
    CHECK(strstr(m.err, "reopen") != NULL, "mask err lacks the remedy: '%s'", m.err);
    CHECK(strstr(f.err, "reopen") != NULL, "f3 err lacks the remedy: '%s'", f.err);
}

static void test_declined_is_distinguishable_from_unknown(void) {
    /* The whole reason OPEN_TIME_ONLY exists. Collapsing these two sends an
     * operator hunting for a typo in a correctly-spelled option. */
    diag_optset_t declined = P("f3=high");
    diag_optset_t unknown  = P("f4=high");
    CHECK(declined.status != unknown.status,
          "declined and unknown share status %d", declined.status);
    CHECK(unknown.status == DIAG_OPTSET_UNKNOWN_KEY, "unknown status %d",
          unknown.status);
}

static void test_an_unknown_key_names_both_vocabularies(void) {
    diag_optset_t o = P("nativedecode=on");
    CHECK(o.status == DIAG_OPTSET_UNKNOWN_KEY, "status %d", o.status);
    CHECK(strstr(o.err, "nativedecode") != NULL, "err omits the key: '%s'", o.err);
    CHECK(strstr(o.err, "rawlog") != NULL, "err omits the settable set: '%s'", o.err);
    /* A key that is open-time-only is still a real option; saying so is what
     * stops the next operator from concluding celldiag has no mask option. */
    CHECK(strstr(o.err, "mask") != NULL, "err omits the open-time set: '%s'", o.err);
}

/* ── syntax ────────────────────────────────────────────────────────────── */
static void test_a_bare_word_is_a_syntax_error(void) {
    diag_optset_t o = P("rawlog");
    CHECK(o.status == DIAG_OPTSET_BAD_SYNTAX, "status %d", o.status);
    CHECK(strstr(o.err, "rawlog") != NULL, "err omits the input: '%s'", o.err);
}

static void test_empty_and_null_are_named_not_crashes(void) {
    diag_optset_t e = P("");
    diag_optset_t n = P(NULL);
    CHECK(e.status == DIAG_OPTSET_BAD_SYNTAX, "empty status %d", e.status);
    CHECK(n.status == DIAG_OPTSET_BAD_SYNTAX, "null status %d", n.status);
    CHECK(n.err[0] != '\0', "null produced no message");
}

static void test_multi_pair_is_refused_rather_than_half_applied(void) {
    /* The framework reports ONE success for the whole string, so there is no
     * way to say "the first applied and the second did not". Refusing is the
     * only answer that cannot lie. */
    const char *forms[] = { "rawlog=/a.hdlc,f3=high", "rawlog=/a.hdlc f3=high",
                            "rawlog=/a.hdlc,mask=full" };
    size_t i;
    for (i = 0; i < sizeof(forms) / sizeof(forms[0]); i++) {
        diag_optset_t o = P(forms[i]);
        CHECK(o.status == DIAG_OPTSET_BAD_SYNTAX,
              "'%s' status %d (expected BAD_SYNTAX)", forms[i], o.status);
    }
}

static void test_a_path_is_not_a_second_setting_just_for_its_characters(void) {
    /* The control for the test above, and it is the sharper of the two.
     * `,`, ` ` and `=` are all legal in a filename, so a multi-pair test spelled
     * "the value contains one of these" refuses paths a person can create --
     * and it refuses them with a message about multiple settings, which points
     * nowhere near the truth. The rule is a delimiter followed by a KNOWN KEY
     * followed by '=', i.e. the SHAPE of a second setting. */
    const char *paths[] = { "/captures/my run.hdlc", "/captures/a=b.hdlc",
                            "/captures/run,2.hdlc", "/captures/f3=notakey.hdlc" };
    size_t i;
    for (i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
        char spec[256];
        diag_optset_t o;
        snprintf(spec, sizeof(spec), "rawlog=%s", paths[i]);
        o = P(spec);
        CHECK(o.status == DIAG_OPTSET_OK, "'%s' status %d (err '%s')",
              spec, o.status, o.err);
        CHECK(strcmp(o.value, paths[i]) == 0,
              "'%s' round-tripped as '%s'", paths[i], o.value);
    }
}

static void test_an_empty_value_points_at_the_disable_spelling(void) {
    diag_optset_t o = P("rawlog=");
    CHECK(o.status == DIAG_OPTSET_BAD_VALUE, "status %d", o.status);
    CHECK(strstr(o.err, "off") != NULL,
          "err does not name the disable spelling: '%s'", o.err);
}

static void test_an_overlong_value_fails_rather_than_truncating(void) {
    char spec[DIAG_OPTSET_VALUE_MAX + 64];
    diag_optset_t o;
    memset(spec, 'x', sizeof(spec) - 1);
    spec[sizeof(spec) - 1] = '\0';
    memcpy(spec, "rawlog=", 7);
    o = P(spec);
    /* Truncating a capture path writes a file nobody can find, and the tee
     * would report success. */
    CHECK(o.status == DIAG_OPTSET_BAD_VALUE, "status %d", o.status);
    CHECK(o.value[0] == '\0', "an overlong value was partially kept: '%s'", o.value);
}

/* ── the caller's value-error channel ──────────────────────────────────── */
static void test_caller_value_error_names_the_callers_accepted_set(void) {
    /* This module must never carry its own copy of F3_PRESETS or the mask
     * table -- two such lists drift apart. The caller passes
     * its own spelling list in. */
    diag_optset_t o = P("rawlog=/x.hdlc");
    diag_optset_set_value_error(&o, "off|all|high|error");
    CHECK(o.status == DIAG_OPTSET_BAD_VALUE, "status %d", o.status);
    CHECK(strstr(o.err, "off|all|high|error") != NULL,
          "err omits the caller's set: '%s'", o.err);
    CHECK(strstr(o.err, "rawlog") != NULL, "err omits the key: '%s'", o.err);
}

static void test_the_key_vocabularies_agree_with_the_table(void) {
    /* A key added to OPTSET_KEYS with no mention in the vocabulary strings is
     * an option nobody can discover; a vocabulary naming a key the table does
     * not have is worse. Cheap two-way check. */
    CHECK(strstr(diag_optset_known_keys(), "rawlog") != NULL, "known lacks rawlog");
    CHECK(strstr(diag_optset_known_keys(), "mask") != NULL, "known lacks mask");
    CHECK(strstr(diag_optset_known_keys(), "f3") != NULL, "known lacks f3");
    CHECK(strstr(diag_optset_runtime_keys(), "rawlog") != NULL, "runtime lacks rawlog");
    /* The declined keys must NOT appear in the runtime set -- that string is
     * what the errors offer as "things you can set right now". */
    CHECK(strstr(diag_optset_runtime_keys(), "mask") == NULL,
          "runtime set advertises mask, which is declined");
    CHECK(strstr(diag_optset_runtime_keys(), "f3") == NULL,
          "runtime set advertises f3, which is declined");
}

int main(void) {
    test_rawlog_path_is_settable();
    test_rawlog_off_and_none_both_disable();
    test_a_path_named_offering_is_not_a_disable();
    test_surrounding_whitespace_is_trimmed();
    test_mask_and_f3_are_declined_not_rejected();
    test_declined_is_distinguishable_from_unknown();
    test_an_unknown_key_names_both_vocabularies();
    test_a_bare_word_is_a_syntax_error();
    test_empty_and_null_are_named_not_crashes();
    test_multi_pair_is_refused_rather_than_half_applied();
    test_a_path_is_not_a_second_setting_just_for_its_characters();
    test_an_empty_value_points_at_the_disable_spelling();
    test_an_overlong_value_fails_rather_than_truncating();
    test_caller_value_error_names_the_callers_accepted_set();
    test_the_key_vocabularies_agree_with_the_table();

    if (failures == 0) {
        printf("PASS: all diag_optset tests\n");
        return 0;
    }
    fprintf(stderr, "FAILED: %d diag_optset test check(s)\n", failures);
    return 1;
}
