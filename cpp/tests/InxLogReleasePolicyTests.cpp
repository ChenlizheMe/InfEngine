#include <core/log/InxLog.h>

#include <cstdlib>
#include <vector>

using namespace infernux;

int main()
{
    InxLog &log = InxLog::GetInstance();
    log.SetLogLevel(LOG_DEBUG);

    std::vector<LogLevel> receivedLevels;
    const size_t sinkId = log.AddSink([&receivedLevels](LogLevel level, const char *, int, const std::string &, bool) {
        receivedLevels.push_back(level);
    });

    int evaluatedDebugArgument = 0;
    INXLOG_DEBUG(++evaluatedDebugArgument);
    log.Log(LOG_DEBUG, __FILE__, __LINE__, "direct debug call");
    INXLOG_INFO("release policy info probe");

    log.RemoveSink(sinkId);

    if (evaluatedDebugArgument != 0)
        return EXIT_FAILURE;
    if (receivedLevels.size() != 1 || receivedLevels.front() != LOG_INFO)
        return EXIT_FAILURE;
    return EXIT_SUCCESS;
}
